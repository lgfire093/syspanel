"""Aimbot trigger engine for the streamer panel.

Modes
-----
* delay : fire button pressed -> after ``delay_ms`` the internal aimbot is
          switched on. Release switches it off.
* drag  : same as delay, but the fire button must be dragged at least
          ``drag_space`` pixels before the aimbot arms. Lower value = less
          drag needed.
* legit : a separate hold key controls the aimbot.
          REAL LEGIT  -> hold = aimbot ON,  release = aimbot OFF
          REVERSE     -> hold = aimbot OFF, release = aimbot ON

The engine only writes to the ESP core through ``internal.set_aimbot_visible``
which is serialized and de-duplicated, so it never spams the pipe.
"""

import ctypes
import math
import threading
import time

import psutil

import internal
import panel_state as ps

try:
    import win32gui
    import win32process
except Exception:  # pragma: no cover - pywin32 should always be present
    win32gui = None
    win32process = None

_user32 = ctypes.windll.user32

VK_MAP = {
    "left": 0x01, "right": 0x02, "middle": 0x04, "x1": 0x05, "x2": 0x06,
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "alt": 0x12, "capslock": 0x14, "esc": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "insert": 0x2D, "delete": 0x2E,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46,
    "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C,
    "m": 0x4D, "n": 0x4E, "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52,
    "s": 0x53, "t": 0x54, "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58,
    "y": 0x59, "z": 0x5A,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
}


def vk_for(name):
    return VK_MAP.get(str(name).strip().lower(), 0x01)


def key_down(name):
    vk = vk_for(name)
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def _cursor_pos():
    pt = ctypes.wintypes.POINT()
    try:
        _user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y
    except Exception:
        return 0, 0


def _distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


_FOCUS_CACHE = {"value": False, "at": 0.0}


def is_hdplayer_in_focus():
    """True when HD-Player is the foreground window (cached ~180 ms)."""
    now = time.time()
    if now - _FOCUS_CACHE["at"] < 0.18:
        return _FOCUS_CACHE["value"]
    _FOCUS_CACHE["at"] = now
    result = False
    if win32gui and win32process:
        try:
            hwnd = win32gui.GetForegroundWindow()
            if hwnd:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                proc = psutil.Process(pid)
                if proc.name().lower() == "hd-player.exe":
                    result = True
        except Exception:
            result = False
    _FOCUS_CACHE["value"] = result
    return result


class AimbotEngine(threading.Thread):
    def __init__(self):
        super().__init__(name="l4g-aimbot-engine", daemon=True)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.mode = "off"          # off | delay | drag
        self.legit = False
        self.reverse = False
        self.last_visible = None

    # -- external configuration -------------------------------------------------
    def set_mode(self, mode):
        mode = str(mode or "off").lower()
        if mode not in ("off", "delay", "drag"):
            mode = "off"
        with self._lock:
            self.mode = mode
        if mode == "off":
            self._release_visible()
        return mode

    def set_legit(self, enabled, reverse=None):
        with self._lock:
            self.legit = bool(enabled)
            if reverse is not None:
                self.reverse = bool(reverse)
        if not self.legit:
            self._release_visible()
        return self.legit

    def set_reverse(self, reverse):
        with self._lock:
            self.reverse = bool(reverse)
        return self.reverse

    def _release_visible(self):
        # Let the loop handle the off transition so it stays serialized.
        pass

    def reset(self):
        with self._lock:
            self.mode = "off"
            self.legit = False
            self.reverse = False
        internal.set_aimbot_visible(False)
        ps.set("aimbot.auto_visible", False)
        ps.set("aimbot.engine_active", False)

    # -- thread -----------------------------------------------------------------
    def stop(self):
        self._stop.set()

    def run(self):
        prev_fire = False
        press_t = 0.0
        drag_accum = 0.0
        prev_pos = None
        armed = False
        desired_last = None
        apply_at = 0.0
        focus_at = 0.0
        focus = False

        while not self._stop.is_set():
            try:
                now = time.time()

                if now - focus_at > 0.18:
                    focus = is_hdplayer_in_focus()
                    focus_at = now

                snap = ps.snapshot()
                aim = snap.get("aimbot", {})
                connected = bool(snap.get("runtime", {}).get("esp_connected"))
                mode = aim.get("mode", "off")
                legit = bool(aim.get("legit"))
                reverse = bool(aim.get("reverse"))
                delay_ms = int(aim.get("delay_ms", 0) or 0)
                drag_space = max(1, int(aim.get("drag_space", 50) or 50))
                trigger_key = aim.get("trigger_key", "left")
                legit_key = aim.get("legit_key", "right")

                desired = None

                if connected and focus:
                    if legit:
                        held = key_down(legit_key)
                        desired = (not held) if reverse else held
                    elif mode in ("delay", "drag"):
                        fire = key_down(trigger_key)
                        if not fire:
                            prev_fire = False
                            armed = False
                            drag_accum = 0.0
                            prev_pos = None
                            desired = False
                        else:
                            if not prev_fire or mode == "delay":
                                if not prev_fire:
                                    press_t = now
                                    armed = (mode == "delay")
                                    drag_accum = 0.0
                                    prev_pos = _cursor_pos()
                            if mode == "drag":
                                pos = _cursor_pos()
                                if prev_pos is not None:
                                    # Drag UPWARD: prev Y minus current Y
                                    drag_up = max(0, prev_pos[1] - pos[1])
                                    drag_accum += drag_up
                                prev_pos = pos
                                threshold = max(5, drag_space)
                                if drag_accum >= threshold:
                                    armed = True
                                else:
                                    armed = False
                            if armed and (now - press_t) * 1000.0 >= delay_ms:
                                desired = True
                            else:
                                desired = False
                            prev_fire = True
                    else:
                        # No automation mode: mirror the manual button.
                        desired = bool(aim.get("visible"))
                else:
                    prev_fire = False
                    armed = False
                    drag_accum = 0.0
                    prev_pos = None
                    if legit or mode in ("delay", "drag"):
                        desired = False
                    elif connected:
                        desired = bool(aim.get("visible"))

                if desired is not None and desired != desired_last:
                    if now - apply_at >= 0.06:
                        internal.set_aimbot_visible(desired)
                        ps.set("aimbot.auto_visible", desired, persist=False)
                        desired_last = desired
                        apply_at = now
                        if desired:
                            ps.log("Trigger active: aimbot ON", "ok")
                        else:
                            ps.log("Trigger released: aimbot OFF", "sys")

                active = bool(legit or mode in ("delay", "drag"))
                ps.set("aimbot.engine_active", active, persist=False)
                ps.set("aimbot.auto_visible", bool(internal.aimbot_visible_state()),
                       persist=False)
            except Exception as exc:  # noqa: BLE001 - never kill the thread
                try:
                    ps.log(f"Engine error: {exc}", "err")
                except Exception:
                    pass
                time.sleep(0.2)
            time.sleep(0.012)


_ENGINE = AimbotEngine()


def start_engine():
    if not _ENGINE.is_alive():
        _ENGINE.start()
    return _ENGINE


def stop_engine():
    try:
        _ENGINE.stop()
    except Exception:
        pass


def engine():
    return _ENGINE


# ---------------------------------------------------------------------------
# Backwards compatible helpers used by the legacy endpoints
# ---------------------------------------------------------------------------

def aimbotv2on():
    _ENGINE.set_mode("drag")
    ps.set("aimbot.mode", "drag")
    return "Autonomic Aimbot Enabled"


def aimbotv2off():
    _ENGINE.set_mode("off")
    ps.set("aimbot.mode", "off")
    return "Autonomic Aimbot Disabled"


def set_autonomic_settings(delay, speed, key="left"):
    """Legacy endpoint: delay(ms) + drag space + trigger key."""
    try:
        delay = max(0, min(1000, int(delay)))
    except Exception:
        delay = 0
    try:
        speed = max(1, min(500, int(speed)))
    except Exception:
        speed = 50
    triggered = str(key or "left").lower()
    if triggered not in VK_MAP:
        triggered = "left"
    ps.set("aimbot.delay_ms", delay)
    ps.set("aimbot.drag_space", speed)
    ps.set("aimbot.trigger_key", triggered)
    internal.set_aim_delay(delay)
    return f"delay={delay} drag={speed} key={triggered}"


# Keep the old flag API for anything still importing it.
def aimbot_loop_enabled():
    return _ENGINE.mode != "off" or _ENGINE.legit
