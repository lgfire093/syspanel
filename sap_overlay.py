"""sap_overlay.py - ESP overlay ported from AIM core OverlayForm.cs.

Raw Win32 GDI implementation (no WinForms dependency):
  * borderless WS_POPUP topmost tool window
  * WS_EX_LAYERED + WS_EX_TRANSPARENT (click-through) + WS_EX_TOOLWINDOW
  * black colorkey transparency via SetLayeredWindowAttributes(LWA_COLORKEY)
  * double-buffered GDI drawing (memory DC -> BitBlt)
  * SetWindowDisplayAffinity(0x11) for STREAMER ESP (invisible to recorders)

Drawing port: FOV circle, rotating crosshair, box / corner box / filled /
3D-style box, snaplines with dot, name, distance, health (6 styles),
weapon, 8-bone skeleton, enemy-count pill, distance fade, same-spot dedup.
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

import psutil

from sap_game import STATE, CFG, matrix_ok, tick_ms, v3_distance

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000

LWA_COLORKEY = 0x1
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11

SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOWNA = 8

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_DISPLAYCHANGE = 0x007E
WM_TIMER = 0x0113
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_MOUSEMOVE = 0x0200

PS_SOLID = 0
PS_NULL = 5
NULL_BRUSH = 5
BLACK_BRUSH = 4
NULL_PEN = 8
TRANSPARENT = 1
FW_BOLD = 700
DEFAULT_CHARSET = 1
OUT_TT_PRECIS = 4
CLIP_DEFAULT_PRECIS = 0
CLEARTYPE_QUALITY = 5
DEFAULT_PITCH = 0

DT_LEFT = 0x00000000
SRCCOPY = 0x00CC0020

GWLP_USERDATA = -21

TIMER_DRAW = 1
TIMER_TRACK = 2
DRAW_MS = 15          # ~66 fps paint: slower timers make the box wobble vs the camera
TRACK_MS = 100
RESCAN_MS = 1000      # pick up a new BlueStacks window after fullscreen switch
GEOMETRY_SETTLE_MS = 350
DISPLAY_SETTLE_MS = 800
LINE_WIDTH = 2        # tracer/snapline pens (was 1 - too thin)

LRESULT = ctypes.c_ssize_t

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
ENUMWINDOWSPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]

_GLOBAL_OVERLAY_INSTANCE = None

def get_overlay_instance():
    global _GLOBAL_OVERLAY_INSTANCE
    return _GLOBAL_OVERLAY_INSTANCE

class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassW.restype = wintypes.ATOM
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.EnumWindows.argtypes = [ENUMWINDOWSPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumChildWindows.argtypes = [wintypes.HWND, ENUMWINDOWSPROC, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH]
user32.FillRect.restype = ctypes.c_int
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.BeginPaint.restype = wintypes.HDC
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.CreatePen.restype = wintypes.HPEN
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wintypes.HGDIOBJ
gdi32.Rectangle.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.RoundRect.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int]
gdi32.Ellipse.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.MoveToEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.LineTo.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.Polyline.argtypes = [wintypes.HDC, ctypes.POINTER(POINT), ctypes.c_int]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.TextOutW.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int]
gdi32.TextOutW.restype = wintypes.BOOL
gdi32.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(SIZE)]
gdi32.GetTextExtentPoint32W.restype = wintypes.BOOL
gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
]
gdi32.CreateFontW.restype = wintypes.HFONT


def _colorref(rgb) -> int:
    return int(rgb[0]) | (int(rgb[1]) << 8) | (int(rgb[2]) << 16)


def _blend(rgb, alpha: int):
    """Emulate alpha over black; never returns pure black (colorkey-safe)."""
    r = (rgb[0] * alpha) // 255
    g = (rgb[1] * alpha) // 255
    b = (rgb[2] * alpha) // 255
    if r == 0 and g == 0 and b == 0:
        return (12, 12, 12)
    return (r, g, b)


_OUTLINE = (12, 12, 12)


EMULATOR_NAMES = (
    "hd-player.exe", "hd-player", "hd-player64.exe", "hd-player64",
    "bluestacks.exe", "bluestacks", "bstk.exe", "bstk", "dnplayer.exe", "dnplayer",
    "nox.exe", "nox", "memu.exe", "memu"
)

def _find_hd_player_pid() -> int:
    candidates = tuple(n.lower() for n in EMULATOR_NAMES)
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name in candidates:
                return int(proc.pid)
        except Exception:
            continue
    return 0


class _MotionCompensator:
    """Per-entity velocity model: predicts positions up to the draw instant.

    Entity data lands at ~50 Hz while the paint timer fires ~66 times/s, so a
    raw sample is always *past* world data. Re-projecting a past point through
    the newest camera matrix is what makes boxes shiver while running or
    turning, and lets them drift off the enemy between scans. Prediction
    removes that age gap; a jump larger than _TELEPORT_M snaps instead.
    """

    _MAX_AGE_MS = 120        # never predict further than this
    _TELEPORT_M = 2.5        # bigger jumps are respawns/tears -> snap, don't lean
    _VEL_ALPHA = 0.35        # EMA weight of the newest velocity sample
    _MAX_VEL = 60.0          # m/s sanity cap (falls) before a sample is ignored
    _MAX_DT_MS = 450         # gap too wide to trust as a velocity baseline

    def __init__(self):
        self._track = {}     # address -> {"tick","head","root","vel"}

    def prune(self, live_addresses) -> None:
        if len(self._track) > 96:
            self._track = {k: v for k, v in self._track.items() if k in live_addresses}

    def predict(self, entity, now) -> "tuple[tuple, tuple, tuple]":
        """Return (extraplated_head, extraplated_root, bone_shift).

        bone_shift is the rigid world-space offset applied to the skeleton
        points so bones ride the same predicted motion as the box.
        """
        addr = entity.address
        sample_t = entity.last_data_ok_tick
        head, root = entity.head, entity.root
        if not addr or not sample_t:
            return head, root, (0.0, 0.0, 0.0)
        if head == (0.0, 0.0, 0.0) or root == (0.0, 0.0, 0.0):
            return head, root, (0.0, 0.0, 0.0)

        state = self._track.get(addr)
        if state is None or sample_t < state["tick"]:
            # New entity, respawn or torn read: start clean, no lean.
            self._track[addr] = {"tick": sample_t, "head": head, "root": root,
                                 "vel": (0.0, 0.0, 0.0)}
            return head, root, (0.0, 0.0, 0.0)

        dt_ms = sample_t - state["tick"]
        if self._MAX_DT_MS >= dt_ms >= 4 and state["head"] != (0.0, 0.0, 0.0):
            dt_s = dt_ms / 1000.0
            raw = ((head[0] - state["head"][0]) / dt_s,
                   (head[1] - state["head"][1]) / dt_s,
                   (head[2] - state["head"][2]) / dt_s)
            speed = (raw[0] * raw[0] + raw[1] * raw[1] + raw[2] * raw[2]) ** 0.5
            if math.isfinite(speed) and speed < self._MAX_VEL:
                vel = state["vel"]
                a = self._VEL_ALPHA
                state["vel"] = (vel[0] + (raw[0] - vel[0]) * a,
                                vel[1] + (raw[1] - vel[1]) * a,
                                vel[2] + (raw[2] - vel[2]) * a)

        jump = v3_distance(head, state["head"])
        teleport = jump > self._TELEPORT_M
        if teleport:
            state["vel"] = (0.0, 0.0, 0.0)
        state["tick"] = sample_t
        state["head"] = head
        state["root"] = root

        age_ms = float(now - sample_t)
        vel = state["vel"]
        if teleport or age_ms <= 0.0 or age_ms > self._MAX_AGE_MS:
            age_ms = 0.0   # teleport: snap to truth. stale: no prediction, raw sample.
        t = age_ms / 1000.0
        head_e = (head[0] + vel[0] * t, head[1] + vel[1] * t, head[2] + vel[2] * t)
        root_e = (root[0] + vel[0] * t, root[1] + vel[1] * t, root[2] + vel[2] * t)
        return head_e, root_e, (vel[0] * t, vel[1] * t, vel[2] * t)


class EspOverlay:
    """Win32 GDI ESP overlay."""

    def __init__(self, log: Optional[Callable[[str], None]] = None):
        global _GLOBAL_OVERLAY_INSTANCE
        _GLOBAL_OVERLAY_INSTANCE = self
        self.phone_esp_hide_pc = False
        self.log = log or (lambda msg: None)
        self._motion = _MotionCompensator()   # welds ESP to the enemy (anti-shake)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.hwnd = None
        self.target_hwnd = None
        self.render_hwnd = None
        self.width = 0
        self.height = 0
        self.rotation_angle = 0.0
        self._streamer_applied = -1
        self._class_name = "SapAimOverlayWnd_%d" % id(self)
        self._wndproc = WNDPROC(self._on_message)
        self._enum_proc = ENUMWINDOWSPROC(self._on_enum_window)
        self._child_proc = ENUMWINDOWSPROC(self._on_enum_child)
        self._fonts = {}
        self._pens = {}
        self._brushes = {}
        self._gdi_objects = []
        self._enum_pid = 0
        self._enum_best = 0
        self._enum_best_area = 0
        self._child_best = 0
        self._child_best_area = 0
        # Reusable GDI back buffer: creating a bitmap + DC every frame was a
        # measurable CPU cost on low-end machines.
        self._buffer_dc = 0
        self._buffer_bitmap = 0
        self._buffer_old_bmp = 0
        self._buffer_w = 0
        self._buffer_h = 0
        self._blank_sent = False
        # V12.9 resize/fullscreen tracking
        self._last_rescan = 0
        self._last_render_resolve = 0
        self._last_size = (0, 0)
        self._last_pos = None
        self._last_geometry_at = 0
        self._suspend_until = 0
        self._hidden_for_geometry = False
        self._draw_ms = DRAW_MS

    # ------------------------------------------------------------------ api

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._main, name="sap-overlay", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._running = False
        hwnd = self.hwnd
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)
        self._thread = None

    # ------------------------------------------------------------- window

    def _main(self) -> None:
        self._set_dpi_aware()
        while self._running:
            try:
                self._create_window()
                self._message_loop()
            except Exception as exc:
                self.log("[overlay] loop exception caught, auto-recovering in 1s: %r" % exc)
                time.sleep(1.0)
            finally:
                self._cleanup()
                self.hwnd = None
            if not self._running:
                break

    @staticmethod
    def _set_dpi_aware() -> None:
        """Make the process per-monitor DPI aware so client rects are physical."""
        try:
            shcore = ctypes.WinDLL("shcore")
            shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass

    def _create_window(self) -> None:
        hinstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinstance
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = self._class_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            err = ctypes.get_last_error()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                raise OSError("RegisterClassW failed (%d)" % err)

        ex_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
        hwnd = user32.CreateWindowExW(
            ex_style, self._class_name, "SAP ESP Overlay", WS_POPUP,
            0, 0, 1, 1, None, None, hinstance, None,
        )
        if not hwnd:
            raise OSError("CreateWindowExW failed (%d)" % ctypes.get_last_error())
        self.hwnd = hwnd
        user32.SetLayeredWindowAttributes(hwnd, _colorref((0, 0, 0)), 255, LWA_COLORKEY)
        self._make_fonts()
        user32.SetTimer(hwnd, TIMER_DRAW, DRAW_MS, None)
        user32.SetTimer(hwnd, TIMER_TRACK, TRACK_MS, None)
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        self.log("[overlay] window created")

    def _make_fonts(self) -> None:
        def make(pt: int):
            return gdi32.CreateFontW(
                -int(round(pt * 96 / 72)), 0, 0, 0, FW_BOLD, 0, 0, 0,
                DEFAULT_CHARSET, OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS,
                CLEARTYPE_QUALITY, DEFAULT_PITCH, "Segoe UI",
            )

        self._fonts = {
            "name": make(10),
            "dist": make(9),
            "weapon": make(8),
            "hp": make(7),
            "status": make(11),
        }
        self._gdi_objects.extend(self._fonts.values())

    def _message_loop(self) -> None:
        msg = MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TIMER:
                if wparam == TIMER_TRACK:
                    self._track_target(hwnd)
                elif wparam == TIMER_DRAW:
                    self._draw_frame(hwnd)
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_PAINT:
                self._noop_paint(hwnd)
                return 0
            if msg == WM_DISPLAYCHANGE:
                self._on_display_change(hwnd)
                return 0
            if msg == WM_CLOSE:
                user32.KillTimer(hwnd, TIMER_DRAW)
                user32.KillTimer(hwnd, TIMER_TRACK)
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as exc:
            self.log("[overlay] message error: %r" % exc)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _noop_paint(self, hwnd) -> None:
        rect = RECT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(rect))
        if hdc:
            user32.EndPaint(hwnd, ctypes.byref(rect))

    # ------------------------------------------------------------ tracking

    def _on_enum_window(self, hwnd, lparam):
        try:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != self._enum_pid:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.IsIconic(hwnd):
                return True
            rect = RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return True
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area > self._enum_best_area:
                self._enum_best_area = area
                self._enum_best = int(hwnd or 0)
        except Exception:
            pass
        return True

    def _find_target(self) -> int:
        pid = _find_hd_player_pid()
        if not pid:
            return 0
        self._enum_pid = pid
        self._enum_best = 0
        self._enum_best_area = 200 * 150
        user32.EnumWindows(self._enum_proc, 0)
        return self._enum_best

    def _on_enum_child(self, hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            rect = RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return True
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area > self._child_best_area:
                self._child_best_area = area
                self._child_best = int(hwnd or 0)
        except Exception:
            pass
        return True

    def _resolve_render_hwnd(self, target: int) -> int:
        parent_rect = RECT()
        if not user32.GetClientRect(target, ctypes.byref(parent_rect)):
            return target
        parent_area = max(0, parent_rect.right - parent_rect.left) * max(0, parent_rect.bottom - parent_rect.top)

        self._child_best = 0
        self._child_best_area = 0
        user32.EnumChildWindows(target, self._child_proc, 0)
        best = self._child_best
        best_area = self._child_best_area
        if best and best_area > parent_area // 4 and best_area > 200 * 150:
            return best
        return target

    def _drop_target(self, hwnd=None) -> None:
        """Forget the stale window and blank the overlay (resize/fullscreen)."""
        self.target_hwnd = None
        self.render_hwnd = None
        self._last_size = (0, 0)
        self._last_pos = None
        self.width = 0
        self.height = 0
        STATE.width = 0
        STATE.height = 0
        self._destroy_buffer()
        self._blank_sent = False
        if hwnd:
            try:
                user32.ShowWindow(hwnd, SW_HIDE)
            except Exception:
                pass

    def _on_display_change(self, hwnd) -> None:
        now = tick_ms()
        self._suspend_until = now + DISPLAY_SETTLE_MS
        self._last_geometry_at = now
        self._hidden_for_geometry = True
        STATE.display_change_tick = now
        STATE.geometry_change_tick = now
        self._drop_target(hwnd)
        self.log("[overlay] display change - re-detecting emulator window")

    def _draw_interval_for(self, w: int, h: int) -> int:
        """Lower the draw rate automatically on very large/fullscreen windows."""
        area = max(0, int(w)) * max(0, int(h))
        if area >= 3500000:      # 2560x1440 and above
            return 50            # 20 fps
        if area >= 1900000:      # 1920x1080
            return 40            # 25 fps
        return DRAW_MS

    def _set_draw_rate(self, hwnd, w: int, h: int) -> None:
        ms = self._draw_interval_for(w, h)
        if ms == self._draw_ms:
            return
        self._draw_ms = ms
        user32.SetTimer(hwnd, TIMER_DRAW, ms, None)

    def _track_target(self, hwnd):
        try:
            now = tick_ms()
            if now < self._suspend_until:
                if not self._hidden_for_geometry:
                    self._hidden_for_geometry = True
                    user32.ShowWindow(hwnd, SW_HIDE)
                return

            target = self.target_hwnd
            target_rect = RECT()
            target_valid = bool(
                target
                and user32.IsWindow(target)
                and user32.IsWindowVisible(target)
                and user32.GetClientRect(target, ctypes.byref(target_rect))
            )
            if target and user32.IsIconic(target):
                self._drop_target(hwnd)
                return
            if not target_valid:
                target = 0

            # While fullscreen, BlueStacks can create a brand-new top-level
            # window and leave the old one alive but hidden/small. Re-enumerate
            # once a second and move to the larger window.
            if target and now - self._last_rescan >= RESCAN_MS:
                self._last_rescan = now
                best = self._find_target()
                if best and best != target:
                    best_rect = RECT()
                    cur_area = max(0, target_rect.right - target_rect.left) * max(
                        0, target_rect.bottom - target_rect.top)
                    if user32.GetClientRect(best, ctypes.byref(best_rect)):
                        best_area = max(0, best_rect.right - best_rect.left) * max(
                            0, best_rect.bottom - best_rect.top)
                        if best_area > 200 * 150 and best_area > cur_area * 1.08:
                            self.log("[overlay] target switched to 0x%X (%dx%d)"
                                     % (best, best_rect.right, best_rect.bottom))
                            target = best
                            target_valid = False

            if not target:
                target = self._find_target()
                if target != self.target_hwnd:
                    self.target_hwnd = target or None
                    self.render_hwnd = None
                    self._last_size = (0, 0)
                    self._last_pos = None
                    self._destroy_buffer()
                if not target:
                    user32.ShowWindow(hwnd, SW_HIDE)
                    return
            elif target != self.target_hwnd:
                had_target = bool(self.target_hwnd)
                self.target_hwnd = target
                self.render_hwnd = None
                self._last_size = (0, 0)
                self._last_pos = None
                self._destroy_buffer()
                if had_target:
                    self._last_geometry_at = now
                    self._hidden_for_geometry = True
                    STATE.geometry_change_tick = now

            render = self.render_hwnd
            if (not render or not user32.IsWindow(render)
                    or now - self._last_render_resolve >= RESCAN_MS):
                self._last_render_resolve = now
                new_render = self._resolve_render_hwnd(target)
                if new_render and user32.IsWindow(new_render):
                    render = new_render
                if not render or not user32.IsWindow(render):
                    render = target
                self.render_hwnd = render

            rect = RECT()
            if not user32.GetClientRect(render, ctypes.byref(rect)):
                self._drop_target(hwnd)
                return
            w = max(0, rect.right - rect.left)
            h = max(0, rect.bottom - rect.top)
            if w <= 10 or h <= 10:
                # Minimized / in transition: hide instead of leaving a stale
                # overlay floating over the desktop. Keep the target so the
                # next track can resume when the window comes back.
                self._hidden_for_geometry = True
                self._destroy_buffer()
                user32.ShowWindow(hwnd, SW_HIDE)
                return

            origin = POINT(rect.left, rect.top)
            if not user32.ClientToScreen(render, ctypes.byref(origin)):
                self._drop_target(hwnd)
                return

            new_size = (int(w), int(h))
            if self._last_size != (0, 0):
                changed = (abs(new_size[0] - self._last_size[0]) > 4
                           or abs(new_size[1] - self._last_size[1]) > 4)
                if changed:
                    self._last_geometry_at = now
                    STATE.geometry_change_tick = now
                    self._hidden_for_geometry = True
                    self._blank_sent = False
                    self._destroy_buffer()
                    user32.ShowWindow(hwnd, SW_HIDE)
            self._last_size = new_size
            self.width = w
            self.height = h
            STATE.width = w
            STATE.height = h
            self._set_draw_rate(hwnd, w, h)

            # Let the emulator finish its resize/fullscreen animation before
            # drawing again. This is the part that used to make ESP vanish or
            # the emulator stutter during the transition.
            if now - self._last_geometry_at < GEOMETRY_SETTLE_MS:
                if not self._hidden_for_geometry:
                    self._hidden_for_geometry = True
                    user32.ShowWindow(hwnd, SW_HIDE)
                return

            pos = (int(origin.x), int(origin.y), int(w), int(h))
            if self._hidden_for_geometry or pos != self._last_pos:
                user32.SetWindowPos(hwnd, HWND_TOPMOST, pos[0], pos[1], pos[2], pos[3],
                                    SWP_NOACTIVATE | SWP_SHOWWINDOW)
                self._last_pos = pos
                self._hidden_for_geometry = False
            else:
                if not getattr(self, "_last_topmost_tick", 0) or now - getattr(self, "_last_topmost_tick", 0) > 500:
                    self._last_topmost_tick = now
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self._apply_streamer(hwnd)
        except Exception as exc:
            self.log("[overlay] track error: %r" % exc)

    def _apply_streamer(self, hwnd) -> None:
        want = 1 if getattr(CFG, "streamer_esp", True) else 0
        if want == self._streamer_applied:
            return
        try:
            if want:
                user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            else:
                user32.SetWindowDisplayAffinity(hwnd, WDA_NONE)
            self._streamer_applied = want
        except Exception:
            pass

    # ------------------------------------------------------------- drawing

    def _gdi_pen(self, rgb, width: int):
        key = (int(rgb[0]), int(rgb[1]), int(rgb[2]), int(width))
        pen = self._pens.get(key)
        if pen is None:
            if len(self._pens) > 400:
                for old in self._pens.values():
                    try:
                        gdi32.DeleteObject(old)
                    except Exception:
                        pass
                    try:
                        self._gdi_objects.remove(old)
                    except ValueError:
                        pass
                self._pens.clear()
            pen = gdi32.CreatePen(PS_SOLID, max(1, int(width)), _colorref(rgb))
            self._pens[key] = pen
            self._gdi_objects.append(pen)
        return pen

    def _gdi_brush(self, rgb):
        key = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        brush = self._brushes.get(key)
        if brush is None:
            if len(self._brushes) > 400:
                for old in self._brushes.values():
                    try:
                        gdi32.DeleteObject(old)
                    except Exception:
                        pass
                    try:
                        self._gdi_objects.remove(old)
                    except ValueError:
                        pass
                self._brushes.clear()
            brush = gdi32.CreateSolidBrush(_colorref(rgb))
            self._brushes[key] = brush
            self._gdi_objects.append(brush)
        return brush

    def _destroy_buffer(self) -> None:
        if self._buffer_dc:
            try:
                if self._buffer_old_bmp:
                    gdi32.SelectObject(self._buffer_dc, self._buffer_old_bmp)
            except Exception:
                pass
            try:
                gdi32.DeleteDC(self._buffer_dc)
            except Exception:
                pass
            self._buffer_dc = 0
        if self._buffer_bitmap:
            try:
                gdi32.DeleteObject(self._buffer_bitmap)
            except Exception:
                pass
            self._buffer_bitmap = 0
        self._buffer_old_bmp = 0
        self._buffer_w = 0
        self._buffer_h = 0

    @staticmethod
    def _any_draw_enabled() -> bool:
        return bool(
            CFG.esp_line or CFG.esp_line_down or CFG.esp_box or CFG.esp_fill
            or CFG.esp_skeleton or CFG.esp_name or CFG.esp_health
            or CFG.esp_distance or CFG.esp_weapon or CFG.esp_crosshair
            or CFG.esp_fov or CFG.esp_enemy_count or CFG.aim_fov_circle
        )

    def _draw_frame(self, hwnd) -> None:
        if not self._running:
            return
        if getattr(self, "phone_esp_hide_pc", False):
            return
        if self._hidden_for_geometry or tick_ms() < self._suspend_until:
            return
        if not self.target_hwnd or not user32.IsWindow(self.target_hwnd):
            return
        w = self.width
        h = self.height
        if w <= 0 or h <= 0:
            return

        # Nothing enabled: clear the overlay once, then stop the 30 fps timer
        # work entirely.  Aimbot-only users used to pay for a full-screen blit
        # every frame with no ESP drawn.
        if not self._any_draw_enabled():
            if self._blank_sent:
                return
            self._blank_sent = True
        else:
            self._blank_sent = False

        if w <= 16 or h <= 16:
            return
        hdc = user32.GetDC(hwnd)
        if not hdc:
            return
        # Clamp buffer dimensions to prevent GDI allocation failures on extreme sizes
        alloc_w = max(16, min(int(w), 3840))
        alloc_h = max(16, min(int(h), 2160))

        if not self._buffer_dc or self._buffer_w != alloc_w or self._buffer_h != alloc_h:
            self._destroy_buffer()
            try:
                self._buffer_dc = gdi32.CreateCompatibleDC(hdc)
                self._buffer_bitmap = gdi32.CreateCompatibleBitmap(hdc, alloc_w, alloc_h)
            except Exception as exc:
                self.log("[overlay] buffer create exception: %r" % exc)
                self._destroy_buffer()
                user32.ReleaseDC(hwnd, hdc)
                return
            if not self._buffer_dc or not self._buffer_bitmap:
                self._destroy_buffer()
                user32.ReleaseDC(hwnd, hdc)
                return
            self._buffer_old_bmp = gdi32.SelectObject(self._buffer_dc, self._buffer_bitmap)
            self._buffer_w = alloc_w
            self._buffer_h = alloc_h

        memdc = self._buffer_dc
        try:
            # black background = transparent (colorkey)
            rect = RECT(0, 0, w, h)
            user32.FillRect(memdc, ctypes.byref(rect), gdi32.GetStockObject(BLACK_BRUSH))
            try:
                self._paint(memdc, w, h)
            except Exception as exc:
                self.log("[overlay] paint error: %r" % exc)
            gdi32.BitBlt(hdc, 0, 0, w, h, memdc, 0, 0, SRCCOPY)
        finally:
            user32.ReleaseDC(hwnd, hdc)


    def set_phone_esp_mode(self, enabled: bool):
        self.phone_esp_hide_pc = bool(enabled)
        if self.hwnd and user32.IsWindow(self.hwnd):
            if self.phone_esp_hide_pc:
                user32.ShowWindow(self.hwnd, SW_HIDE)
            else:
                user32.ShowWindow(self.hwnd, SW_SHOWNA)

    def capture_stream_frame(self, quality: int = 50) -> bytes:
        """Capture live emulator window + active ESP overlay in real-time (0-delay hardware scaling)."""
        target = self.render_hwnd or self.target_hwnd
        if not target or not user32.IsWindow(target):
            target = self._find_target()
            if target and user32.IsWindow(target):
                self.target_hwnd = target
                resolved = self._resolve_render_hwnd(target)
                if resolved and user32.IsWindow(resolved):
                    self.render_hwnd = resolved
                    target = resolved
        if not target or not user32.IsWindow(target) or user32.IsIconic(target):
            return None

        rect = RECT()
        if not user32.GetClientRect(target, ctypes.byref(rect)):
            return None
        w = max(16, rect.right - rect.left)
        h = max(16, rect.bottom - rect.top)

        # Convert target client (0, 0) to absolute screen coordinates
        pt = POINT(0, 0)
        user32.ClientToScreen(target, ctypes.byref(pt))

        # Stream resolution for mobile phone display (0 delay)
        out_w = 854
        out_h = 480

        hdc_screen = user32.GetDC(0)
        if not hdc_screen:
            return None

        mem_dc = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        old_bmp = gdi32.SelectObject(mem_dc, hbmp)

        scale_dc = gdi32.CreateCompatibleDC(hdc_screen)
        scale_bmp = gdi32.CreateCompatibleBitmap(hdc_screen, out_w, out_h)
        old_scale_bmp = gdi32.SelectObject(scale_dc, scale_bmp)

        try:
            # 1. Capture emulator frame (Try direct window DC first, fallback to bounded screen BitBlt)
            captured = False
            hdc_target = user32.GetDC(target)
            if hdc_target:
                try:
                    if gdi32.BitBlt(mem_dc, 0, 0, w, h, hdc_target, 0, 0, SRCCOPY):
                        captured = True
                finally:
                    user32.ReleaseDC(target, hdc_target)

            if not captured:
                if user32.PrintWindow(target, mem_dc, 2):
                    captured = True

            if not captured:
                gdi32.BitBlt(mem_dc, 0, 0, w, h, hdc_screen, pt.x, pt.y, SRCCOPY)

            user32.ReleaseDC(0, hdc_screen)
            hdc_screen = None

            # 2. Paint active ESP layers directly onto the game frame for phone viewing
            try:
                self._paint(mem_dc, w, h)
            except Exception:
                pass

            # 3. Hardware GDI scaling to 854x480 (sub-millisecond scaling)
            gdi32.SetStretchBltMode(scale_dc, 3)
            gdi32.StretchBlt(scale_dc, 0, 0, out_w, out_h, mem_dc, 0, 0, w, h, SRCCOPY)

            # 4. Extract pre-scaled 854x480 pixels with zero Python resizing overhead
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = out_w
            bmi.bmiHeader.biHeight = -out_h
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0

            if not hasattr(self, '_stream_buf') or len(self._stream_buf) != (out_w * out_h * 4):
                self._stream_buf = ctypes.create_string_buffer(out_w * out_h * 4)

            gdi32.GetDIBits(scale_dc, scale_bmp, 0, out_h, self._stream_buf, ctypes.byref(bmi), 0)

            import PIL.Image
            import io

            img = PIL.Image.frombuffer('RGBA', (out_w, out_h), self._stream_buf, 'raw', 'BGRA', 0, 1)
            bio = io.BytesIO()
            img.convert('RGB').save(bio, format='JPEG', quality=quality)
            return bio.getvalue()
        except Exception:
            return None
        finally:
            if hdc_screen:
                user32.ReleaseDC(0, hdc_screen)
            gdi32.SelectObject(scale_dc, old_scale_bmp)
            gdi32.DeleteObject(scale_bmp)
            gdi32.DeleteDC(scale_dc)
            gdi32.SelectObject(mem_dc, old_bmp)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem_dc)

    # -- text helpers -------------------------------------------------------

    def _measure(self, hdc, text: str, font) -> int:
        old = gdi32.SelectObject(hdc, font)
        size = SIZE()
        gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(size))
        gdi32.SelectObject(hdc, old)
        return int(size.cx)

    def _text(self, hdc, text: str, font_key: str, rgb, x: float, y: float, outline: bool = True) -> None:
        font = self._fonts[font_key]
        old_font = gdi32.SelectObject(hdc, font)
        try:
            if outline:
                gdi32.SetTextColor(hdc, _colorref(_OUTLINE))
                for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    gdi32.TextOutW(hdc, int(round(x)) + ox, int(round(y)) + oy, text, len(text))
            gdi32.SetTextColor(hdc, _colorref(rgb))
            gdi32.TextOutW(hdc, int(round(x)), int(round(y)), text, len(text))
        finally:
            gdi32.SelectObject(hdc, old_font)

    def _line(self, hdc, pen, x1, y1, x2, y2) -> None:
        old_pen = gdi32.SelectObject(hdc, pen)
        gdi32.MoveToEx(hdc, int(round(x1)), int(round(y1)), None)
        gdi32.LineTo(hdc, int(round(x2)), int(round(y2)))
        gdi32.SelectObject(hdc, old_pen)

    def _rect(self, hdc, pen, x, y, w, h) -> None:
        old_pen = gdi32.SelectObject(hdc, pen)
        old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(NULL_BRUSH))
        gdi32.Rectangle(hdc, int(round(x)), int(round(y)), int(round(x + w)), int(round(y + h)))
        gdi32.SelectObject(hdc, old_brush)
        gdi32.SelectObject(hdc, old_pen)

    def _fill_rect(self, hdc, brush, x, y, w, h) -> None:
        old_brush = gdi32.SelectObject(hdc, brush)
        old_pen = gdi32.SelectObject(hdc, gdi32.GetStockObject(NULL_PEN))
        gdi32.Rectangle(hdc, int(round(x)), int(round(y)), int(round(x + w)), int(round(y + h)))
        gdi32.SelectObject(hdc, old_pen)
        gdi32.SelectObject(hdc, old_brush)

    def _ellipse(self, hdc, pen, brush, x, y, w, h) -> None:
        old_pen = gdi32.SelectObject(hdc, pen if pen else gdi32.GetStockObject(NULL_PEN))
        old_brush = gdi32.SelectObject(hdc, brush if brush else gdi32.GetStockObject(NULL_BRUSH))
        gdi32.Ellipse(hdc, int(round(x)), int(round(y)), int(round(x + w)), int(round(y + h)))
        gdi32.SelectObject(hdc, old_brush)
        gdi32.SelectObject(hdc, old_pen)

    # -- main paint ---------------------------------------------------------

    def _paint(self, hdc, w: int, h: int) -> None:
        gdi32.SetBkMode(hdc, TRANSPARENT)
        mx = w / 2.0
        my = h / 2.0

        # FOV circle (AIMFOVS -> CFG.aim_fov_circle; legacy esp_fov stays hard-off)
        if CFG.esp_fov or CFG.aim_fov_circle:
            fov_radius = float(CFG.fov_radius)
            fov_col = _blend(CFG.color_fov, 220)
            self._ellipse(hdc, self._gdi_pen(fov_col, 2), None,
                          mx - fov_radius, my - fov_radius, fov_radius * 2, fov_radius * 2)

        # crosshair - rotating diamond
        if CFG.esp_crosshair:
            self.rotation_angle += 0.1
            if self.rotation_angle >= 360.0:
                self.rotation_angle -= 360.0
            angle = math.pi * self.rotation_angle / 180.0
            size = 10.0
            cos_v = math.cos(angle)
            sin_v = math.sin(angle)
            ch_col = CFG.color_crosshair

            glow_pen = self._gdi_pen(_blend(ch_col, 80), 4)
            self._line(hdc, glow_pen, mx + size * cos_v, my - size * sin_v, mx - size * cos_v, my + size * sin_v)
            self._line(hdc, glow_pen, mx - size * sin_v, my - size * cos_v, mx + size * sin_v, my + size * cos_v)

            pen = self._gdi_pen(ch_col, 2)
            self._line(hdc, pen, mx + size * cos_v, my - size * sin_v, mx - size * cos_v, my + size * sin_v)
            self._line(hdc, pen, mx - size * sin_v, my - size * cos_v, mx + size * sin_v, my + size * cos_v)
            self._ellipse(hdc, None, self._gdi_brush(_blend((255, 255, 255), 200)),
                          mx - 1.5, my - 1.5, 3, 3)

        enemy_count = 0
        drawn_spots = []
        matrix = STATE.camera_matrix
        now = int(time.time() * 1000)

        # Keep drawing from the last good matrix for a short grace period.
        # have_matrix used to flip False on one missed read, which blanked the
        # whole ESP for a frame (the "blink" while moving).
        matrix_tick = int(getattr(STATE, "matrix_tick", 0) or 0)
        if not STATE.have_matrix:
            if not matrix_tick or now - matrix_tick > 2000:
                return
        if not matrix_ok(matrix):
            return

        try:
            self._motion.prune({ent.address for ent in STATE.entity_snapshot if ent.address})
        except Exception:
            pass
        for entity in STATE.entity_snapshot:
            if not entity.is_known or entity.is_dead:
                continue
            if entity.is_team == "true":
                continue
            if entity.head == (0.0, 0.0, 0.0) and entity.root == (0.0, 0.0, 0.0):
                continue
            if now - entity.last_data_ok_tick > 750:
                # A slow memory scan must never leave boxes projected from old
                # bone positions: hide the stale box instead of drawing it off
                # the enemy. Normal scans refresh every 20 ms.
                continue

            dist = entity.distance
            limit = float(CFG.esp_max_distance) if CFG.esp_max_distance > 0 else 300.0
            if not math.isfinite(dist) or dist <= 0.0:
                # No trustworthy distance this frame: keep drawing instead of
                # culling every enemy (a bad camera read used to hide all ESP).
                dist = 0.0
                has_distance = False
            else:
                has_distance = True
                if dist > 1000.0 or dist < 0.05:
                    continue
                if dist > max(1.0, limit):
                    continue
            if entity.health < 1 or entity.health > 200:
                continue

            # Predicted to the draw instant: erases the 20 ms sample gap that
            # made boxes lag, shiver and wander off the enemy while moving.
            try:
                head_e, root_e, shift = self._motion.predict(entity, now)
            except Exception:
                head_e, root_e, shift = entity.head, entity.root, (0.0, 0.0, 0.0)
            head2d = self._w2s(matrix, head_e, w, h)
            root2d = self._w2s(matrix, root_e, w, h)
            if head2d is None or root2d is None:
                continue

            hx, hy = head2d
            rx, ry = root2d
            if hx < 5 or hy < 5 or hx > w - 5 or hy > h - 5:
                continue
            if rx < 5 or ry < 5 or rx > w - 5 or ry > h - 5:
                continue

            box_height = abs(hy - ry)
            if box_height < 6 or box_height > h * 0.75:
                continue
            box_width = box_height * 0.65
            if box_width < 4 or box_width > w * 0.75:
                continue

            cx = hx
            top_y = hy
            left = cx - box_width / 2
            right = left + box_width
            bottom = top_y + box_height

            overlap = False
            min_sep = max(6.0, box_height * 0.45)
            for spot in drawn_spots:
                if math.hypot(spot[0] - cx, spot[1] - top_y) < min_sep:
                    overlap = True
                    break
            if overlap:
                continue
            drawn_spots.append((cx, top_y))

            knocked = entity.is_knocked
            if knocked:
                box_col_base = (255, 60, 60)
            else:
                box_col_base = CFG.color_box

            fade_alpha = 255
            if has_distance and CFG.esp_max_distance > 0:
                t = dist / CFG.esp_max_distance
                if t > 0.7:
                    fade_alpha = int(255 * max(0.15, min(1.0, 1.0 - (t - 0.7) / 0.3)))
            # Quantize the fade so the GDI pen/brush cache stays small (one pen
            # per base colour instead of 200+ alpha variants).
            fade_alpha = max(16, min(255, (fade_alpha // 32) * 32))
            box_col = _blend(box_col_base, fade_alpha)
            enemy_count += 1

            box_style = int(getattr(CFG, "box_style", 0))

            # BOX
            if CFG.esp_box:
                if box_style == 1:
                    self._corner_box(hdc, self._gdi_pen(box_col, 2), left, top_y, box_width, box_height)
                elif box_style == 2:
                    fill = _blend(CFG.color_fill, 55)
                    self._fill_rect(hdc, self._gdi_brush(fill), left, top_y, box_width, box_height)
                    self._rect(hdc, self._gdi_pen(box_col, 2), left, top_y, box_width, box_height)
                elif box_style == 3:
                    self._rect(hdc, self._gdi_pen(box_col, 2), left, top_y, box_width, box_height)
                    self._rect(hdc, self._gdi_pen(_blend(box_col, 90), 1),
                               left + 3, top_y + 3, box_width - 6, box_height - 6)
                else:
                    self._rect(hdc, self._gdi_pen(box_col, 2), left, top_y, box_width, box_height)

            # SNAPLINE (CFG.esp_line -> modes 1 top / 2 center / 3 bottom)
            if CFG.esp_line:
                if knocked:
                    line_col = _blend(CFG.color_knocked, min(220, fade_alpha))
                else:
                    line_col = _blend(CFG.color_line, min(220, fade_alpha))
                if CFG.esp_mode == "1":
                    line_start_y = 10.0
                elif CFG.esp_mode == "2":
                    line_start_y = my
                else:
                    line_start_y = h - 10.0
                self._line(hdc, self._gdi_pen(line_col, LINE_WIDTH), mx, line_start_y, cx, top_y)
                if CFG.esp_mode in ("1", "3"):
                    self._ellipse(hdc, self._gdi_pen(_blend((0, 0, 0), 0), LINE_WIDTH),
                                  self._gdi_brush(_blend((255, 80, 80), 220)),
                                  mx - 4, line_start_y - 4, 8, 8)

            # SNAPLINE legacy (bottom)
            if CFG.esp_line_down:
                sc = CFG.color_knocked if knocked else CFG.color_line_down
                self._line(hdc, self._gdi_pen(_blend(sc, 200 if knocked else 190), LINE_WIDTH),
                           mx, h, cx, top_y)

            # NAME
            if CFG.esp_name and entity.name:
                name_col = _blend(CFG.color_name, min(255, fade_alpha))
                tw = self._measure(hdc, entity.name, self._fonts["name"])
                self._text(hdc, entity.name, "name", name_col, cx - tw / 2, top_y - 17)

            # DISTANCE
            if CFG.esp_distance and has_distance:
                dist_text = "%dm" % round(dist)
                tw = self._measure(hdc, dist_text, self._fonts["dist"])
                self._text(hdc, dist_text, "dist", _blend((255, 220, 100), fade_alpha),
                           cx - tw / 2, bottom + 2)

            # HEALTH
            if CFG.esp_health:
                self._draw_health(hdc, entity, box_height, top_y, right, cx, knocked)

            # WEAPON
            if CFG.esp_weapon and entity.weapon_name:
                tw = self._measure(hdc, entity.weapon_name, self._fonts["weapon"])
                self._text(hdc, entity.weapon_name, "weapon", _blend((220, 230, 255), 255),
                           cx - tw / 2, bottom + 14)

            # SKELETON
            if CFG.esp_skeleton:
                if knocked:
                    skel_col = _blend((255, 80, 80), 200)
                else:
                    skel_col = _blend(CFG.color_skeleton, 180)
                self._bones(hdc, self._gdi_pen(skel_col, 2), entity, shift)

        # ENEMY COUNT pill
        if CFG.esp_enemy_count:
            text = "Enemy Count - %d" % enemy_count
            tw = self._measure(hdc, text, self._fonts["status"])
            bx = mx - tw / 2 - 8
            by = 22.0
            bw = tw + 16
            bh = 22.0
            bg = _blend((15, 20, 30), 180)
            border = _blend(CFG.color_enemy_count, 200)
            old_pen = gdi32.SelectObject(hdc, self._gdi_pen(border, 1))
            old_brush = gdi32.SelectObject(hdc, self._gdi_brush(bg))
            gdi32.RoundRect(hdc, int(bx), int(by), int(bx + bw), int(by + bh), 16, 16)
            gdi32.SelectObject(hdc, old_brush)
            gdi32.SelectObject(hdc, old_pen)
            self._text(hdc, text, "status", CFG.color_enemy_count, bx + 8, by + 3)

    # -- sub-draws ----------------------------------------------------------

    def _w2s(self, matrix, pos, w, h):
        v9 = pos[0] * matrix[0] + pos[1] * matrix[4] + pos[2] * matrix[8] + matrix[12]
        v10 = pos[0] * matrix[1] + pos[1] * matrix[5] + pos[2] * matrix[9] + matrix[13]
        v12 = pos[0] * matrix[3] + pos[1] * matrix[7] + pos[2] * matrix[11] + matrix[15]
        if v12 >= 0.001:
            half_w = w / 2.0
            half_h = h / 2.0
            return (half_w + (half_w * v9) / v12, half_h - (half_h * v10) / v12)
        return None

    def _corner_box(self, hdc, pen, x, y, w, h) -> None:
        corner_len = min(w, h) / 3.0
        self._line(hdc, pen, x, y + corner_len, x, y)
        self._line(hdc, pen, x, y, x + corner_len, y)
        self._line(hdc, pen, x + w - corner_len, y, x + w, y)
        self._line(hdc, pen, x + w, y, x + w, y + corner_len)
        self._line(hdc, pen, x, y + h - corner_len, x, y + h)
        self._line(hdc, pen, x, y + h, x + corner_len, y + h)
        self._line(hdc, pen, x + w - corner_len, y + h, x + w, y + h)
        self._line(hdc, pen, x + w, y + h, x + w, y + h - corner_len)

    def _draw_health(self, hdc, entity, box_height, top_y, right, cx, knocked) -> None:
        style = int(getattr(CFG, "health_style", 0))
        hp = max(0, min(200, entity.health))
        pct = max(0.0, min(1.0, hp / 200.0))

        if knocked:
            bar_col = (255, 80, 80)
        elif pct > 0.6:
            bar_col = self._lerp3((0, 255, 51), (255, 217, 0), (1.0 - pct) * 2.5)
        else:
            bar_col = self._lerp3((255, 217, 0), (255, 38, 38), (0.6 - pct) * 1.66)

        draw_bar = style in (0, 1, 4)
        draw_segments = style == 5
        draw_text = style in (2, 3, 4)

        if draw_bar or draw_segments:
            bar_x = right + 5
            bar_y = top_y
            bar_w = 3.0
            # dark track
            self._fill_rect(hdc, self._gdi_brush(_blend((0, 0, 0), 140)), bar_x - 1, bar_y - 1,
                            bar_w + 2, box_height + 2)
            if draw_segments:
                seg_h = box_height / 10.0
                filled = int(round(pct * 10))
                for i in range(10):
                    seg_y = bar_y + i * seg_h
                    if 10 - i <= filled:
                        self._fill_rect(hdc, self._gdi_brush(bar_col), bar_x, seg_y + 0.5,
                                        bar_w, max(1.0, seg_h - 1.5))
            else:
                bar_h = box_height * pct
                self._fill_rect(hdc, self._gdi_brush(bar_col), bar_x, bar_y + box_height - bar_h,
                                bar_w, bar_h)
                if bar_h > 1:
                    self._fill_rect(hdc, self._gdi_brush(_blend((255, 255, 255), 220)),
                                    bar_x, bar_y + box_height - bar_h, bar_w, 1)

        if draw_text:
            if style == 3:
                text = "%d%%" % int(round(pct * 100))
            else:
                text = "%d HP" % hp
            tw = self._measure(hdc, text, self._fonts["hp"])
            name_y = top_y - 2
            if CFG.esp_name and entity.name:
                name_y = top_y - 17
            self._text(hdc, text, "hp", bar_col, cx - tw / 2, name_y - 11)

    @staticmethod
    def _lerp3(a, b, t):
        t = max(0.0, min(1.0, t))
        return (
            int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t),
        )

    def _bones(self, hdc, pen, entity, shift=(0.0, 0.0, 0.0)) -> None:
        pairs = (
            (entity.head, entity.breast),
            (entity.breast, entity.hip),
            (entity.breast, entity.left_shoulder),
            (entity.breast, entity.right_shoulder),
            (entity.left_shoulder, entity.left_hand),
            (entity.right_shoulder, entity.right_hand),
            (entity.hip, entity.left_foot),
            (entity.hip, entity.right_foot),
        )
        matrix = STATE.camera_matrix
        w = STATE.width
        h = STATE.height
        for a, b in pairs:
            if a == (0.0, 0.0, 0.0) or b == (0.0, 0.0, 0.0):
                continue
            if shift != (0.0, 0.0, 0.0):
                a = (a[0] + shift[0], a[1] + shift[1], a[2] + shift[2])
                b = (b[0] + shift[0], b[1] + shift[1], b[2] + shift[2])
            s1 = self._w2s(matrix, a, w, h)
            s2 = self._w2s(matrix, b, w, h)
            if s1 is None or s2 is None:
                continue
            margin = 300
            if s1[0] < -margin or s1[1] < -margin or s1[0] > w + margin or s1[1] > h + margin:
                continue
            if s2[0] < -margin or s2[1] < -margin or s2[0] > w + margin or s2[1] > h + margin:
                continue
            self._line(hdc, pen, s1[0], s1[1], s2[0], s2[1])

    def _cleanup(self) -> None:
        self._destroy_buffer()
        for obj in self._gdi_objects:
            try:
                gdi32.DeleteObject(obj)
            except Exception:
                pass
        self._gdi_objects.clear()
        self._pens.clear()
        self._brushes.clear()
        self._fonts.clear()
