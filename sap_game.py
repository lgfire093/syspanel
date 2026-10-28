"""sap_game.py - game data layer ported from AIM core.

Port of:  Offsets.cs, Config.cs, Entity.cs, Bones.cs, Transform.cs, W2S.cs,
          Data.cs (Work thread), AimbotAi.cs (aimbot visible), WeaponIndex.cs

Reads the Free Fire entity list, bone positions, health, names, weapons and
the view matrix through sap_mem, and keeps a thread-safe GameState that the
ESP overlay and the aimbot modes consume.
"""

from __future__ import annotations

import ctypes
import math
import os
import struct as _struct
import tempfile
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

from sap_mem import Memory

# ESP diagnostics: one line every 2s (tick rate, matrix age, entity counts).
# Kept in %TEMP% so a frozen EXE can always write it; capped at 512 KB.
_DIAG_PATH = os.path.join(tempfile.gettempdir(), "l4g_esp_diag.log")
# Quiet default: no diag file on disk unless L4G_LOGS=1.
_DIAG_ENABLED = os.environ.get("L4G_LOGS", "0") == "1"
_DIAG_MAX_BYTES = 512 * 1024

# ---------------------------------------------------------------------------
# Offsets.cs (OB55 Free Fire India / Global - OFFSETANDBONES.txt)
# ---------------------------------------------------------------------------

# ================= CORE / BASE =================
# Free Fire India (com.garena.game.ffi) uses 0xA33ED84
# Free Fire Global / TH / Max uses 0xA342EFC
INIT_BASE_FFI = 0xA33ED84
INIT_BASE_GLOBAL = 0xA342EFC
INIT_BASE = 0xA33ED84

GAME_FACADE_SCRIPT = 0xA5BC344
STATIC_CLASS = 0x5C
ACCESS_STATIC_CLASS = 0x5C

CURRENT_MATCH = 0x50
M_MATCH = 0x50
CURRENT_MATCH_GAME = 0x4
MATCH_STATUS = 0x8C
MATCH_STATE = 0x8C

LOCAL_PLAYER = 0x94
LOCAL_PLAYER_ATTRIB = 0x500
DICTIONARY_ENTITIES = 0x68

# ================= PLAYER =================
PLAYER_IS_DEAD = 0x50
PLAYER_NAME = 0x31C
PLAYER_DATA = 0x48
PLAYER_DATA_POOL = 0x48
PLAYER_SHADOW_BASE = 0x1A60
LOCAL_PLAYER_ATTRIBUTES = 0x500
XPOSE = 0x78
AVATAR_MANAGER = 0x504
AVATAR = 0xA8
AVATAR_IS_VISIBLE = 0x95
AVATAR_DATA = 0x14
AVATAR_DATA_IS_TEAM = 0x59
IS_CLIENT_BOT = 0x324

# ================= CAMERA / AIM =================
VIEW_MATRIX = 0xE8
FOLLOW_CAMERA = 0x494
CAMERA = 0x18
CAMERA_INTPTR = 0x8
MAIN_CAMERA_TRANSFORM = 0x28C
AIM_ROTATION = 0x440
HEAD_COLLIDER_ALT = 0x18
SAFE_AIM_ROTATION = 0x440
INSTANT_AIM_ROTATION = 0x131C
ROTATION_CALL_COUNTER = 0x5A8
LOCAL_PLAYER_IS_FIRING = 0x58C
SAIM1 = 0x58C
SAIM2 = 0x9D0
SAIM3 = 0x38
SAIM4 = 0x2C

# ================= WEAPON =================
WEAPON = 0x434
WEAPON_DATA = 0x58
WEAPON_INFO_TYPE = 0x64
WEAPON_RECOIL = 0xC
WEAPON_RECOIL_SCATTER = 0xC
RAPIDFIRE_PTR = 0x1C8
NO_RELOAD = 0xC1
NO_RELOAD_PTR = 0xC1

# ================= COLLIDER =================
LOCKED_AIMING_COLLIDER = 0x54
PORUL = 0x54
HEAD_COLLIDER = 0x4E8
COLLIDER = 0x4E8
AIMBOT_VISIBLE = 0x4E8

# ================= MISC =================
GAME_TIMER = 0x10
FIXED_DELTA_TIME = 0x24
LEVEL = 0x14

# ---------------------------------------------------------------------------
# Bones.cs (OFFSETANDBONES.txt - Free Fire India kBoneSets[2])
# ---------------------------------------------------------------------------

# Head / Spine / Core
B_HEAD = 0x49C
B_HIP = 0x4A0
B_NECK = 0x4A4       # Neck in dump (Spine in BoneSet: 0x4A4)
B_SPINE = 0x4A4
B_BREAST = 0x4A8     # Alias from notes
B_GROIN = 0x4AC
B_ROOT = 0x4B0
B_ROOT_PES = 0x4B0

# Legs
B_LEFT_ANKLE = 0x4B8
B_RIGHT_ANKLE = 0x4BC
B_LEFT_FOOT = 0x4C0
B_LEFT_TOE = 0x4C0
B_RIGHT_FOOT = 0x4C4
B_RIGHT_TOE = 0x4C4

# Arms
B_LEFT_SHOULDER = 0x4D0
B_RIGHT_SHOULDER = 0x4D4
B_LEFT_HAND = 0x4C8
B_RIGHT_HAND = 0x498
B_RIGHT_WRIST_JOINT = 0x498
B_LEFT_ELBOW = 0x4E4
B_RIGHT_ELBOW = 0x4E0
B_LEFT_WRIST = 0x4DC
B_LEFT_WRIST_JOINT = 0x4DC
B_RIGHT_WRIST = 0x4D8

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Aimbot throttling. The C# original loops as fast as the CPU allows because a
# native write is cheap. In Python each silent-aim iteration costs a read + 10
# page-translated writes; running that flat-out starved the data thread, so the
# camera matrix arrived late and the ESP shook/drifted while the player moved.
# A 60 fps game only needs a handful of re-writes per frame.
AIM_INTERVAL = 0.016          # ~62 Hz for VISIBLE / DRAG / DELAY
LEGIT_INTERVAL = 0.020        # ~50 Hz for LEGIT

# The entity scan reads ~30 players plus bone chains and can take 50-400 ms in
# Python (occasionally seconds when a guest page must be re-mapped).  The view
# matrix used to be read at the start of that scan, so the overlay projected
# every box through a matrix that was stale for the whole tick: the boxes
# lagged behind camera rotation, which is the shake/drift seen while moving.
# A dedicated thread now samples only the tiny camera chain at high rate.
CAMERA_INTERVAL = 0.016       # ~62 Hz view-matrix sampling
CAMERA_ANCHOR_MS = 60         # main-camera position refresh (distance anchor)
CAMERA_HIP_MS = 150           # local-player hip refresh (distance fallback)
MATRIX_GRACE_MS = 1200        # keep drawing with the last matrix this long

# Low-end PCs: never scan entities faster than the ESP can draw them.  The old
# loop spun as fast as Python could read (600+ Hz in the lobby), which stole
# CPU from BlueStacks and lowered the game FPS for no visual benefit.
DATA_INTERVAL = 0.020         # in-match entity scan cap (~50 Hz) - keeps ESP glued
DATA_IDLE_INTERVAL = 0.25     # lobby / loading (~4 Hz)

# A window resize / fullscreen switch makes BlueStacks remap guest pages. The
# game clears LocalPlayer for a few seconds while it re-creates the match view;
# resetting the match on the first missed read used to stop the camera thread
# and blank ESP/aimbot. Keep retrying this long before declaring the match gone.
LOCAL_PLAYER_GRACE_MS = 5000
MATCH_GRACE_MS = 3500
CACHE_CLEAR_MIN_MS = 1200      # never thrash the page-table cache
CACHE_REFRESH_MS = 1000        # soft VA->host refresh (keeps chunk memo)
AIM_RESIZE_SETTLE_MS = 400     # don't aim while the emulator is resizing

THREAD_PRIORITY_ABOVE_NORMAL = 1
THREAD_PRIORITY_BELOW_NORMAL = -1


def _set_current_thread_priority(delta: int) -> None:
    """Bias the scheduler so the ESP/data thread wins against the aimbot."""
    try:
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), int(delta))
    except Exception:
        pass

# ---------------------------------------------------------------------------
# WeaponIndex.cs
# ---------------------------------------------------------------------------

_WEAPONS = {
    0: "AK47", 1: "FIST", 2: "M4A1", 3: "USP", 4: "AWM", 5: "M1014",
    6: "AK47", 7: "UMP", 8: "MP5", 9: "Desert-Eagle", 10: "G18", 11: "M14",
    12: "SCAR", 13: "VSS", 14: "GROZA", 15: "MP40", 16: "PAN", 17: "PARANG",
    18: "SKS", 19: "M249", 20: "M1873", 21: "KAR98K", 24: "FAMAS", 25: "M500",
    26: "SVD", 27: "BAT", 28: "XM8", 29: "SPAS12", 30: "M60", 32: "P90",
    33: "AN94", 34: "KATANA", 35: "CG15", 39: "PLASMA", 41: "M1887",
    43: "THOMPSON", 45: "M82B", 46: "AUG", 47: "PARAFAL", 48: "WOODPECKER",
    49: "VECTOR", 50: "MAG7", 51: "SCYTHE", 54: "KORD", 55: "M1917",
    56: "USP2", 57: "KINGFISHER", 58: "MINI-UZI", 60: "MP5", 61: "M60",
    62: "VSS", 63: "M14", 64: "KAR98K", 65: "AWM-Y", 67: "FAMAS-I",
    70: "GROZA", 71: "M249", 72: "SVD", 73: "G36", 74: "G36", 75: "M24",
    78: "HEALSNIPER", 80: "M4A1", 81: "M4A1", 82: "M4A1", 86: "CHARGE BUSTER",
    88: "MAC10", 89: "AC80", 93: "HEAL-PISTOL", 99: "SHIELD-GUN",
    100: "FLAMTHROWER", 119: "M1887", 120: "MP5", 121: "MP5", 122: "M60",
    123: "M60", 124: "VSS", 125: "VSS", 126: "M14", 127: "M14",
    128: "KAR98K", 129: "KAR98K", 130: "FAMAS", 131: "FAMAS", 181: "TROGON",
    150: "BIZON", 197: "VSK94", 178: "SCAR", 179: "SCAR", 180: "SCAR",
    193: "AUG", 194: "AUG", 195: "AUG", 228: "MAC10", 229: "MAC10",
    230: "MAC10", 184: "M1014", 185: "M1014", 186: "M1014",
}


def weapon_name(index: int) -> str:
    return _WEAPONS.get(index, "Weapon[%d]" % index)


# ---------------------------------------------------------------------------
# Config.cs - runtime options (driven by the panel UI)
# ---------------------------------------------------------------------------

class Cfg:
    """Mirror of AIM core Config + panel extras."""

    def __init__(self):
        # aimbot
        self.aim_mode = "off"            # off | visible | delay | drag | legit
        self.aimbot_fov = 500.0          # radius around crosshair (px)
        self.aim_max_distance = 150.0    # metres
        self.ignore_knocked = False
        self.delay_ms = 0                # DELAY MODE
        self.drag_space = 50             # DRAG MODE activation radius (px)
        self.trigger_key = "left"        # fire button for DELAY / DRAG
        self.legit_key = "right"
        self.legit_reverse = False       # LEGIT MODE reverse
        self.smoothness_ms = 0
        self.aim_fov_circle = False      # AIMFOVS button - draw the aim FOV circle

        # no recoil (SNIPER & EXTRA button)
        self.no_recoil = False

        # ESP toggles
        self.esp_line = False
        self.esp_line_down = False
        self.esp_box = False
        self.esp_fill = False
        self.esp_skeleton = False
        self.esp_name = False
        self.esp_health = False
        self.esp_distance = False
        self.esp_weapon = False
        self.esp_crosshair = False
        self.esp_fov = False
        self.esp_enemy_count = False
        self.esp_mode = "1"              # 1 top, 2 center(my), 3 bottom
        self.esp_max_distance = 200.0    # 0 = no limit
        self.line_thickness = 2
        self.box_style = 0               # 0 normal, 1 corner, 2 3D
        self.health_style = 0            # 0 bar, 1 gradient, 2 text, 3 pct, 4 bar+text, 5 seg
        self.fov_radius = 200.0

        # colors (R, G, B)
        self.color_line = (255, 255, 255)
        self.color_line_down = (255, 255, 255)
        self.color_box = (41, 219, 255)
        self.color_skeleton = (41, 219, 255)
        self.color_name = (255, 255, 255)
        self.color_fill = (255, 0, 0)
        self.color_fov = (255, 90, 100)
        self.color_crosshair = (255, 100, 100)
        self.color_enemy_count = (255, 110, 115)
        self.color_knocked = (255, 30, 60)

        # streamer
        self.streamer_esp = True

    def set_color(self, name: str, value: str) -> None:
        """value like '#29dbff' or '29dbff'."""
        text = (value or "").strip().lstrip("#")
        if len(text) != 6:
            return
        try:
            r = int(text[0:2], 16)
            g = int(text[2:4], 16)
            b = int(text[4:6], 16)
        except ValueError:
            return
        setattr(self, name, (r, g, b))


CFG = Cfg()


# ---------------------------------------------------------------------------
# Entity + shared state
# ---------------------------------------------------------------------------

class Entity:
    __slots__ = (
        "address", "is_known", "is_team", "head", "neck", "root", "left_hand",
        "right_hand", "eye", "left_shoulder", "right_shoulder", "left_foot",
        "right_foot", "left_elbow", "right_elbow", "hip", "left_ankle",
        "right_ankle", "breast", "left_leg", "right_leg", "health", "is_dead",
        "is_knocked", "name", "distance", "is_firing", "is_visible", "is_bot",
        "last_seen_tick", "last_data_ok_tick", "avatar", "weapon_name",
    )

    def __init__(self):
        self.address = 0
        self.is_known = False
        self.is_team = "unknown"      # true | false | unknown
        self.head = (0.0, 0.0, 0.0)
        self.neck = (0.0, 0.0, 0.0)
        self.root = (0.0, 0.0, 0.0)
        self.left_hand = (0.0, 0.0, 0.0)
        self.right_hand = (0.0, 0.0, 0.0)
        self.eye = (0.0, 0.0, 0.0)
        self.left_shoulder = (0.0, 0.0, 0.0)
        self.right_shoulder = (0.0, 0.0, 0.0)
        self.left_foot = (0.0, 0.0, 0.0)
        self.right_foot = (0.0, 0.0, 0.0)
        self.left_elbow = (0.0, 0.0, 0.0)
        self.right_elbow = (0.0, 0.0, 0.0)
        self.hip = (0.0, 0.0, 0.0)
        self.left_ankle = (0.0, 0.0, 0.0)
        self.right_ankle = (0.0, 0.0, 0.0)
        self.breast = (0.0, 0.0, 0.0)
        self.left_leg = (0.0, 0.0, 0.0)
        self.right_leg = (0.0, 0.0, 0.0)
        self.health = 0
        self.is_dead = True
        self.is_knocked = False
        self.name = ""
        self.distance = 0.0
        self.is_firing = False
        self.is_visible = False
        self.is_bot = False
        self.last_seen_tick = 0
        self.last_data_ok_tick = 0
        self.avatar = 0
        self.weapon_name = ""


class GameState:
    def __init__(self):
        self.width = 0
        self.height = 0
        self.have_matrix = False
        self.camera_matrix = (0.0,) * 16
        self.local_player = 0
        self.local_main_camera = (0.0, 0.0, 0.0)
        self.playerpos = (0.0, 0.0, 0.0)
        self.match_status = 0
        self.current_match = 0
        self.il2cpp = 0
        self.cr3 = 0
        self.entity_snapshot = []
        self.entity_count = 0
        self.enemy_count = 0
        self.last_failure = ""
        self.last_data_tick = 0
        # --- anti-freeze / diagnostics (moving-ESP fix) --------------------
        self.matrix_tick = 0              # last time a valid matrix was stored
        self.matrix_changes = 0           # how many times the matrix changed
        self.matrix_stale_since = 0       # first tick the matrix stopped moving
        self.chain_fail = 0               # consecutive camera-chain read failures
        self.tick_last_ms = 0             # duration of the last full scan
        self.tick_hz = 0.0                # scans per second (rolling)
        self.last_cam_pos = (0.0, 0.0, 0.0)
        self.cam_moved_tick = 0           # last time the camera actually moved
        self.cam_hz = 0.0                 # camera thread samples per second
        self.cam_tick = 0                 # last camera thread sample (ms)
        self.display_change_tick = 0      # resolution/fullscreen transition (ms)
        self.geometry_change_tick = 0     # emulator client resize (ms)


STATE = GameState()


def tick_ms() -> int:
    return int(time.time() * 1000)


def v3_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def v3_distance(a, b) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def vec3_ok(pos) -> bool:
    """Reject NaN/inf/absurd coordinates coming from a torn or stale read."""
    if not pos or len(pos) != 3:
        return False
    for value in pos:
        if not isinstance(value, (int, float)):
            return False
        if not math.isfinite(value) or abs(value) > 100000.0:
            return False
    return True


def matrix_ok(matrix) -> bool:
    """A Unity view-projection matrix must be finite and non-degenerate.

    Without this check a failed read that returns zeros (or garbage) is stored
    as the camera matrix and every ESP box disappears until the next good read.
    """
    if not matrix or len(matrix) != 16:
        return False
    for value in matrix:
        if not isinstance(value, (int, float)):
            return False
        if not math.isfinite(value) or abs(value) > 10000000.0:
            return False
    if abs(matrix[0]) + abs(matrix[5]) + abs(matrix[10]) < 1.0e-6:
        return False
    if abs(matrix[3]) + abs(matrix[7]) + abs(matrix[11]) + abs(matrix[15]) < 1.0e-6:
        return False
    return True


# ---------------------------------------------------------------------------
# Transform.cs
# ---------------------------------------------------------------------------

def struct_f(data: bytes, offset: int) -> float:
    return _struct.unpack_from("<f", data, offset)[0]


def _read_vec3(mem: Memory, address: int):
    data = mem.read(address, 12)
    if not data:
        return None
    return (struct_f(data, 0), struct_f(data, 4), struct_f(data, 8))


def transform_get_node_position(mem: Memory, node_transform: int):
    """Transform.GetNodePosition - reads the collider transform at +0x8."""
    value = mem.read_u32(node_transform + 0x8)
    if value is None:
        return None
    return transform_get_position(mem, value)


def transform_get_position(mem: Memory, transform: int):
    """Transform.GetPosition - walks the transform hierarchy (port of Transform.cs)."""
    if transform == 0:
        return None
    transform_obj = mem.read_u32(transform + 0x8)
    if not transform_obj:
        return None
    index_value = mem.read_u32(transform_obj + 0x24)
    if index_value is None:
        return None
    matrix_value = mem.read_u32(transform_obj + 0x20)
    if not matrix_value:
        return None
    matrix_list_value = mem.read_u32(matrix_value + 0x18)
    if matrix_list_value is None:
        return None
    matrix_indices_value = mem.read_u32(matrix_value + 0x1C)
    if matrix_indices_value is None:
        return None

    result = _read_vec3(mem, index_value * 0x30 + matrix_list_value)
    if result is None:
        return None

    max_tries = 50
    tries = 0

    transform_index_value = mem.read_i32(index_value * 4 + matrix_indices_value)
    if transform_index_value is None:
        return None

    while transform_index_value >= 0:
        tries += 1
        if tries == max_tries:
            break

        data = mem.read(0x30 * transform_index_value + matrix_list_value, 0x30)
        if not data:
            return None
        px = struct_f(data, 0x00)
        py = struct_f(data, 0x04)
        pz = struct_f(data, 0x08)
        rx = struct_f(data, 0x10)
        ry = struct_f(data, 0x14)
        rz = struct_f(data, 0x18)
        rw = struct_f(data, 0x1C)
        sx = struct_f(data, 0x20)
        sy = struct_f(data, 0x24)
        sz = struct_f(data, 0x28)

        scale_x = result[0] * sx
        scale_y = result[1] * sy
        scale_z = result[2] * sz

        result = (
            px + scale_x
            + (scale_x * ((ry * ry * -2.0) - (rz * rz * 2.0)))
            + (scale_y * ((rw * rz * -2.0) - (ry * rx * -2.0)))
            + (scale_z * ((rz * rx * 2.0) - (rw * ry * -2.0))),
            py + scale_y
            + (scale_x * ((rx * ry * 2.0) - (rw * rz * -2.0)))
            + (scale_y * ((rz * rz * -2.0) - (rx * rx * 2.0)))
            + (scale_z * ((rw * rx * -2.0) - (rz * ry * -2.0))),
            pz + scale_z
            + (scale_x * ((rw * ry * -2.0) - (rx * rz * -2.0)))
            + (scale_y * ((ry * rz * 2.0) - (rw * rx * -2.0)))
            + (scale_z * ((rx * rx * -2.0) - (ry * ry * 2.0))),
        )

        transform_index_value = mem.read_i32(transform_index_value * 4 + matrix_indices_value)
        if transform_index_value is None:
            return None

    if tries == max_tries:
        return None
    return result


# ---------------------------------------------------------------------------
# W2S.cs
# ---------------------------------------------------------------------------

def world_to_screen(matrix, pos, width: int, height: int):
    v9 = pos[0] * matrix[0] + pos[1] * matrix[4] + pos[2] * matrix[8] + matrix[12]
    v10 = pos[0] * matrix[1] + pos[1] * matrix[5] + pos[2] * matrix[9] + matrix[13]
    v12 = pos[0] * matrix[3] + pos[1] * matrix[7] + pos[2] * matrix[11] + matrix[15]

    if v12 >= 0.001:
        half_w = width / 2.0
        half_h = height / 2.0
        return (half_w + (half_w * v9) / v12, half_h - (half_h * v10) / v12)
    return (-1.0, -1.0)


# ---------------------------------------------------------------------------
# Data.cs - Work thread
# ---------------------------------------------------------------------------

class DataWorker(threading.Thread):
    def __init__(self, mem: Memory, log: Optional[Callable[[str], None]] = None):
        super().__init__(name="sap-data", daemon=True)
        self.mem = mem
        self.log = log or (lambda msg: None)
        self._stop = threading.Event()
        self.dict_entities = {}
        self.failed_read_count = 0
        self.frame_count = 0
        self.logged_once = False
        self._last_diag = 0
        self._last_match_miss = 0
        self._last_lp_miss = 0
        self._last_cache_clear = 0
        self._last_translation_refresh = 0
        self._last_display_tick = 0
        self._last_geometry_tick = 0

    def stop(self):
        self._stop.set()

    def _clear_caches(self, now: int, force: bool = False) -> None:
        """Clear stale VA/page translations without thrashing the hypervisor."""
        if not force and now - self._last_cache_clear < CACHE_CLEAR_MIN_MS:
            return
        self._last_cache_clear = now
        self.mem.clear_caches()

    def reset_cache(self):
        STATE.have_matrix = False
        STATE.matrix_tick = 0
        STATE.chain_fail = 0
        STATE.camera_matrix = (0.0,) * 16
        STATE.match_status = 0
        self._last_match_miss = 0
        self._last_lp_miss = 0
        self.dict_entities.clear()
        STATE.entity_snapshot = []
        STATE.entity_count = 0

    def _reset_entities(self, reason: str):
        self.dict_entities.clear()
        STATE.entity_snapshot = []
        STATE.entity_count = 0
        STATE.enemy_count = 0
        self.log("[data] entities reset (%s)" % reason)

    def _diag(self) -> None:
        """Write one compact line every 2s to %TEMP%\\l4g_esp_diag.log."""
        now = tick_ms()
        if now - self._last_diag < 2000:
            return
        self._last_diag = now
        if not _DIAG_ENABLED:
            # no-trace mode: purge any diag file a previous run left behind
            try:
                if os.path.exists(_DIAG_PATH):
                    os.remove(_DIAG_PATH)
            except Exception:
                pass
            return
        try:
            matrix_age = (now - STATE.matrix_tick) if STATE.matrix_tick else -1
            cam_idle = (now - STATE.cam_moved_tick) if STATE.cam_moved_tick else -1
            line = (
                "ts=%d tick=%dms hz=%.1f matrix_age=%dms changes=%d "
                "cam_idle=%dms have_matrix=%s chain_fail=%d ents=%d enemies=%d "
                "cam_hz=%.1f\n"
                % (
                    now, STATE.tick_last_ms, STATE.tick_hz, matrix_age,
                    STATE.matrix_changes, cam_idle, STATE.have_matrix,
                    STATE.chain_fail, STATE.entity_count, STATE.enemy_count,
                    STATE.cam_hz,
                )
            )
            if os.path.exists(_DIAG_PATH) and os.path.getsize(_DIAG_PATH) > _DIAG_MAX_BYTES:
                os.remove(_DIAG_PATH)
            with open(_DIAG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass

    def run(self):
        _set_current_thread_priority(THREAD_PRIORITY_ABOVE_NORMAL)
        self.log("[data] Data thread started")
        # The data thread must stay responsive: unknown chunks are queued for
        # the background resolver instead of blocking this thread for seconds.
        try:
            self.mem.set_fast_mode(True)
        except Exception:
            pass
        time.sleep(0.5)

        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                self._tick()
            except Exception as exc:  # never kill the thread on a read hiccup
                self.log("[data] tick error: %r" % exc)
                time.sleep(0.02)
            elapsed = (time.perf_counter() - started) * 1000.0
            STATE.tick_last_ms = int(elapsed)
            if elapsed > 0.0:
                STATE.tick_hz = 1000.0 / elapsed
            self._diag()

            # Cap the scan rate.  A match needs ~30 updates/s (the ESP draws at
            # 30 fps); the lobby only needs a slow poll to notice a new match.
            if STATE.current_match and self.dict_entities:
                interval = DATA_INTERVAL
            elif STATE.current_match:
                interval = DATA_INTERVAL * 3.0
            else:
                interval = DATA_IDLE_INTERVAL
            spare = interval - (time.perf_counter() - started)
            if spare > 0.0:
                time.sleep(spare)

    def _tick(self):
        time.sleep(0.001)
        now_tick = tick_ms()

        if not self.mem.is_valid:
            return

        # Soft-refresh the VA->host memo once a second. BlueStacks can migrate
        # guest pages while streaming assets; without this, bone positions can
        # stay frozen on the old host page while the camera keeps moving, which
        # looks like every ESP box drifting off the enemy for several seconds.
        if now_tick - self._last_translation_refresh >= CACHE_REFRESH_MS:
            self._last_translation_refresh = now_tick
            try:
                self.mem.clear_translations()
            except Exception:
                pass

        # Resolution / fullscreen / window-size changes are the exact moments
        # BlueStacks remaps guest pages. Drop stale translations immediately so
        # the camera chain can be re-walked instead of failing for a minute.
        display_tick = int(getattr(STATE, "display_change_tick", 0) or 0)
        if display_tick > self._last_display_tick:
            self._last_display_tick = display_tick
            self._clear_caches(now_tick, force=True)
            self._reset_entities("display change")
            STATE.have_matrix = False
            STATE.matrix_tick = 0
        geometry_tick = int(getattr(STATE, "geometry_change_tick", 0) or 0)
        if geometry_tick > self._last_geometry_tick:
            self._last_geometry_tick = geometry_tick
            self._clear_caches(now_tick)

        base_game_facade = self.mem.read_u32(STATE.il2cpp + INIT_BASE)
        if not base_game_facade:
            alt_base = INIT_BASE_GLOBAL if INIT_BASE == INIT_BASE_FFI else INIT_BASE_FFI
            alt_facade = self.mem.read_u32(STATE.il2cpp + alt_base)
            if alt_facade:
                INIT_BASE = alt_base
                base_game_facade = alt_facade

        if not base_game_facade:
            self.failed_read_count += 1
            if self.failed_read_count > 30:
                self._clear_caches(now_tick, force=True)
                self.reset_cache()
                self.failed_read_count = 0
            if not self.logged_once:
                self.log("[data] InitBase read failed (il2cpp=0x%X + 0x%X)" % (STATE.il2cpp, INIT_BASE))
                self.logged_once = True
            return

        if self.logged_once:
            self.log("[data] InitBase OK: 0x%X" % base_game_facade)
            self.logged_once = False
        self.failed_read_count = 0

        game_facade = self.mem.read_u32(base_game_facade)
        if not game_facade:
            return
        static_game_facade = self.mem.read_u32(game_facade + STATIC_CLASS)
        if not static_game_facade:
            return
        current_game = self.mem.read_u32(static_game_facade)
        if not current_game:
            return
        current_match = self.mem.read_u32(current_game + CURRENT_MATCH)
        if not current_match:
            STATE.match_status = 0
            if STATE.current_match != 0:
                if self._last_match_miss == 0:
                    self._last_match_miss = now_tick
                if now_tick - self._last_match_miss < MATCH_GRACE_MS:
                    # Game is re-creating the match view (common after a resize
                    # or fullscreen switch). Keep scanning the last known match
                    # instead of freezing every entity position for 3.5 s.
                    current_match = STATE.current_match
                else:
                    self._clear_caches(now_tick, force=True)
                    self._reset_entities("left match (CurrentMatch=0)")
                    STATE.current_match = 0
                    STATE.have_matrix = False
                    STATE.matrix_tick = 0
                    return
            if not current_match:
                return
        self._last_match_miss = 0

        if STATE.current_match != current_match:
            self._clear_caches(now_tick, force=True)
            self._reset_entities("match changed 0x%X -> 0x%X" % (STATE.current_match, current_match))
            STATE.current_match = current_match
            STATE.have_matrix = False
            STATE.matrix_tick = 0
            STATE.chain_fail = 0
            self._last_lp_miss = 0

        local_player = self.mem.read_u32(current_match + LOCAL_PLAYER)
        if not local_player:
            observer = self.mem.read_u32(current_match + CURRENT_OBSERVER)
            if observer:
                observer_player = self.mem.read_u32(observer + OBSERVER_PLAYER)
                if observer_player:
                    local_player = observer_player

        if not local_player:
            if STATE.current_match != 0:
                if self._last_lp_miss == 0:
                    self._last_lp_miss = now_tick
                missed_ms = now_tick - self._last_lp_miss
                # One failed read is normal while BlueStacks reinitialises the
                # game after a window resize. Keep the entity scan alive with
                # the last known local player instead of freezing all boxes.
                if missed_ms >= 700:
                    self._clear_caches(now_tick)
                if missed_ms < LOCAL_PLAYER_GRACE_MS:
                    local_player = STATE.local_player
                else:
                    self._clear_caches(now_tick, force=True)
                    self._reset_entities("left match (localPlayer=0 for %.1fs)" % (missed_ms / 1000.0))
                    STATE.current_match = 0
                    STATE.have_matrix = False
                    STATE.matrix_tick = 0
                    return
            if not local_player:
                STATE.have_matrix = False
                STATE.matrix_tick = 0
                return
        self._last_lp_miss = 0

        STATE.local_player = local_player

        # No recoil (Data.cs:163-175)
        if CFG.no_recoil:
            weapon = self.mem.read_u32(local_player + WEAPON)
            if weapon:
                weapon_data = self.mem.read_u32(weapon + WEAPON_DATA)
                if weapon_data:
                    recoil = self.mem.read_f32(weapon_data + WEAPON_RECOIL)
                    if recoil:
                        self.mem.write_f32(weapon_data + WEAPON_RECOIL, 0.0)

        # Camera matrix, main-camera position and local hip are sampled by
        # CameraWorker at ~133 Hz. This scan used to read the matrix first and
        # then spend 50-400 ms (sometimes seconds) on entities/bones, so the
        # matrix was stale for the whole tick and every box lagged behind
        # camera rotation - the shake/drift seen while moving with an aimbot
        # mode ON. A slow scan can no longer freeze the ESP projection.

        seen_this_scan = set()
        entities = self.dict_entities

        for entity_addr in self._get_entities(current_game, DICTIONARY_ENTITIES):
            if entity_addr == 0 or entity_addr == local_player:
                continue
            seen_this_scan.add(entity_addr)

            player = entities.get(entity_addr)
            if player is None:
                player = Entity()
                player.address = entity_addr
                player.is_team = "unknown"
                player.is_known = False
                player.is_dead = True
                player.health = 0
                player.is_knocked = False
                player.head = (0.0, 0.0, 0.0)
                player.root = (0.0, 0.0, 0.0)
                player.name = ""
                player.last_seen_tick = now_tick
                entities[entity_addr] = player
                continue

            player.address = entity_addr
            player.last_seen_tick = now_tick

            if player.is_team == "true":
                continue

            if player.is_team == "unknown":
                avatar_ok = False
                avatar_manager = self.mem.read_u32(entity_addr + AVATAR_MANAGER)
                if avatar_manager:
                    avatar = self.mem.read_u32(avatar_manager + AVATAR)
                    if avatar:
                        player.avatar = avatar
                        is_visible = self.mem.read_u8(avatar + AVATAR_IS_VISIBLE)
                        if is_visible:
                            avatar_data = self.mem.read_u32(avatar + AVATAR_DATA)
                            if avatar_data:
                                is_team = self.mem.read_u8(avatar_data + AVATAR_DATA_IS_TEAM)
                                if is_team is not None:
                                    avatar_ok = True
                                    if is_team:
                                        player.is_team = "true"
                                    else:
                                        player.is_team = "false"
                                        player.is_known = True
                if not avatar_ok and not player.is_known:
                    player.is_known = True
                    player.is_team = "false"

            if not player.is_known:
                continue

            if CFG.ignore_knocked:
                shadow_base = self.mem.read_u32(entity_addr + PLAYER_SHADOW_BASE)
                if shadow_base:
                    xpose = self.mem.read_i32(shadow_base + XPOSE)
                    if xpose is not None:
                        player.is_knocked = xpose == 8

            is_dead = self.mem.read_u8(entity_addr + PLAYER_IS_DEAD)
            if is_dead is not None:
                player.is_dead = bool(is_dead)

            data_pool = self.mem.read_u32(entity_addr + PLAYER_DATA)
            if data_pool:
                pool_obj = self.mem.read_u32(data_pool + 0x8)
                if pool_obj:
                    if CFG.esp_weapon:
                        weapon_found = False
                        pool = self.mem.read_u32(pool_obj + 0x20)
                        if pool:
                            weapon_id = self.mem.read_i16(pool + 0x10)
                            if weapon_id:
                                player.weapon_name = weapon_name(weapon_id)
                                weapon_found = True
                        if not weapon_found:
                            pool_alt = self.mem.read_u32(pool_obj + 0x10)
                            if pool_alt:
                                weapon_id = self.mem.read_i16(pool_alt + 0x10)
                                if weapon_id:
                                    player.weapon_name = weapon_name(weapon_id)

                    if CFG.esp_name:
                        name_addr = self.mem.read_u32(entity_addr + PLAYER_NAME)
                        if name_addr:
                            name_len = self.mem.read_i32(name_addr + 0x8)
                            if name_len and 0 < name_len <= 64:
                                name = self.mem.read_string(name_addr + 0xC, name_len * 2, unicode=True)
                                if name:
                                    player.name = name

                    pool = self.mem.read_u32(pool_obj + 0x10)
                    if pool:
                        health_addr = self.mem.read_u32(pool + 0xC)
                        if health_addr:
                            health = self.mem.read_i16(health_addr)
                            if health is not None:
                                player.health = health
                        else:
                            health_short = self.mem.read_i16(pool + 0x10)
                            if health_short is not None:
                                player.health = health_short

            # Bones. Dead players are not drawn or aimed at, so reading their
            # 8-bone chains only stole CPU from the game.
            if not player.is_dead:
                self._read_entity_bones(player, entity_addr, now_tick)

        # Ghost sweep
        for addr in list(entities.keys()):
            if addr not in seen_this_scan and now_tick - entities[addr].last_seen_tick > 2000:
                entities.pop(addr, None)

        STATE.entity_snapshot = list(entities.values())
        STATE.entity_count = len(entities)
        STATE.enemy_count = sum(1 for e in entities.values() if not e.is_dead)
        STATE.last_data_tick = now_tick
        self.frame_count += 1

    # Bone pointers all live in one contiguous block of the Player object
    # (0x498 .. 0x4E8). One 0x50-byte read replaces 10-20 pointer reads per
    # entity per scan, which is what let the data thread fall behind and serve
    # a frozen camera matrix while the player was moving.
    _BONE_BLOCK_START = 0x498
    _BONE_BLOCK_SIZE = 0x50

    def _read_bone_pointers(self, entity_addr: int):
        try:
            data = self.mem.read(entity_addr + self._BONE_BLOCK_START, self._BONE_BLOCK_SIZE)
            if data and len(data) == self._BONE_BLOCK_SIZE:
                return _struct.unpack_from("<20I", data)
        except Exception:
            pass
        return None

    def _bone_ptr(self, slots, entity_addr: int, offset: int) -> int:
        if slots is not None:
            index = (offset - self._BONE_BLOCK_START) >> 2
            if 0 <= index < len(slots):
                return slots[index]
        return self.mem.read_u32(entity_addr + offset) or 0

    def _bone_position(self, slots, entity_addr: int, offset: int):
        bone = self._bone_ptr(slots, entity_addr, offset)
        if not bone:
            return None
        pos = transform_get_node_position(self.mem, bone)
        if pos and vec3_ok(pos):
            return pos
        return None

    def _entity_distance(self, head) -> float:
        cam = STATE.local_main_camera
        if not vec3_ok(cam):
            cam = STATE.playerpos
        if not vec3_ok(cam):
            return float("nan")
        return v3_distance(cam, head)

    def _read_entity_bones(self, player: Entity, entity_addr: int, now_tick: int):
        """Read head/root always and the rest only when the skeleton is drawn.

        Reading 12 bone chains per entity per scan was the main source of the
        data-thread stall; head+root are all the box/snapline/name need.
        """
        slots = self._read_bone_pointers(entity_addr)
        fresh = False

        head = self._bone_position(slots, entity_addr, B_HEAD)
        if head is not None:
            player.head = head
            distance = self._entity_distance(head)
            if math.isfinite(distance):
                player.distance = distance
            # The box is built from the head, so only a good head read proves
            # this entity is still being tracked. A stale head must age out
            # instead of freezing the box at its last position.
            fresh = True

        root = self._bone_position(slots, entity_addr, B_ROOT)
        if root is not None:
            player.root = root

        if not CFG.esp_skeleton:
            if fresh:
                player.last_data_ok_tick = now_tick
            return

        breast = self._bone_position(slots, entity_addr, B_SPINE)
        if breast is not None:
            player.breast = breast
            fresh = True

        hip = self._bone_position(slots, entity_addr, B_HIP)
        if hip is not None:
            player.hip = hip

        left_hand = self._bone_position(slots, entity_addr, B_LEFT_HAND)
        if left_hand is not None:
            player.left_hand = left_hand

        right_hand = self._bone_position(slots, entity_addr, B_RIGHT_WRIST_JOINT)
        if right_hand is not None:
            player.right_hand = right_hand

        left_shoulder = self._bone_position(slots, entity_addr, B_LEFT_SHOULDER)
        if left_shoulder is not None:
            player.left_shoulder = left_shoulder

        right_shoulder = self._bone_position(slots, entity_addr, B_RIGHT_SHOULDER)
        if right_shoulder is not None:
            player.right_shoulder = right_shoulder

        left_foot = self._bone_position(slots, entity_addr, B_LEFT_FOOT)
        if left_foot is not None:
            player.left_foot = left_foot

        right_foot = self._bone_position(slots, entity_addr, B_RIGHT_FOOT)
        if right_foot is not None:
            player.right_foot = right_foot

        left_elbow = self._bone_position(slots, entity_addr, B_LEFT_ELBOW)
        if left_elbow is not None:
            player.left_elbow = left_elbow

        right_elbow = self._bone_position(slots, entity_addr, B_RIGHT_ELBOW)
        if right_elbow is not None:
            player.right_elbow = right_elbow

        left_ankle = self._bone_position(slots, entity_addr, B_LEFT_ANKLE)
        if left_ankle is not None:
            player.left_ankle = left_ankle

        right_ankle = self._bone_position(slots, entity_addr, B_RIGHT_ANKLE)
        if right_ankle is not None:
            player.right_ankle = right_ankle

        if fresh:
            player.last_data_ok_tick = now_tick

    def _get_entities(self, base_game: int, offset: int):
        """Data.cs GetEntities - walks the entity dictionary."""
        mem = self.mem
        result = []

        dictionary = mem.read_u32(base_game + offset)
        if not dictionary:
            return result

        count = mem.read_i32(dictionary + 0x10)
        if count is None or count < 1 or count > 1000:
            return result

        entries = mem.read_u32(dictionary + 0xC)
        if not entries:
            return result

        start = entries + 0x10
        for i in range(count):
            entry = start + i * 0x10
            entry_hash = mem.read_i32(entry)
            if entry_hash is None or entry_hash < 0:
                continue
            entity = mem.read_u32(entry + 0xC)
            if not entity:
                continue
            result.append(entity)

        # Duplicate-slot guard (keep first occurrence only)
        seen = set()
        unique = []
        for item in result:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique


# ---------------------------------------------------------------------------
# Fast camera / view-matrix sampler
# ---------------------------------------------------------------------------

class CameraWorker(threading.Thread):
    """Sample the camera chain at high rate, independently of the entity scan.

    Only five tiny reads per sample:
        current_match -> local_player -> FollowCamera -> Camera
        -> camera+0x8 (fresh walk) -> view matrix (fresh 64-byte read)

    The two ``read_fresh`` calls are the V12.5 anti-freeze fix: BlueStacks can
    remap guest pages while streaming assets, and the memoized VA->host page
    would otherwise keep returning the old matrix. Because this thread does
    nothing else, a slow entity scan (or an aimbot burst) can no longer make
    the matrix stale for hundreds of milliseconds.
    """

    def __init__(self, mem: Memory, log: Optional[Callable[[str], None]] = None):
        super().__init__(name="sap-camera", daemon=True)
        self.mem = mem
        self.log = log or (lambda msg: None)
        self._stop = threading.Event()
        self._last_local = 0
        self._last_anchor = 0
        self._last_hip = 0
        self._matrix_logged = False
        self._samples = 0
        self._hz_tick = tick_ms()
        self._last_display_tick = 0

    def stop(self):
        self._stop.set()

    def _miss(self, reason: str) -> None:
        STATE.chain_fail += 1
        now = tick_ms()
        age = (now - STATE.matrix_tick) if STATE.matrix_tick else 10 ** 9
        if age > MATRIX_GRACE_MS and STATE.have_matrix:
            STATE.have_matrix = False
        # A long chain outage after resize/fullscreen means stale guest-page
        # translations. Retry with a clean page walk a few times instead of
        # waiting for the slower data thread.
        if STATE.current_match and STATE.chain_fail in (30, 100, 300, 900, 1800):
            self.mem.clear_caches()
        if STATE.chain_fail == 1 or STATE.chain_fail % 500 == 0:
            self.log("[camera] matrix miss (%s, x%d)" % (reason, STATE.chain_fail))

    def _anchor(self, local_player: int, now: int) -> None:
        """Refresh the local player anchors (distance labels, target order).

        These do not affect box placement, so they only need ~8-20 Hz; doing
        them here keeps the entity scan lighter.
        """
        if now - self._last_hip >= CAMERA_HIP_MS:
            self._last_hip = now
            hip_bone = self.mem.read_u32(local_player + B_ROOT)
            if hip_bone:
                hip_pos = transform_get_node_position(self.mem, hip_bone)
                if hip_pos and vec3_ok(hip_pos):
                    STATE.playerpos = hip_pos

        if now - self._last_anchor >= CAMERA_ANCHOR_MS:
            self._last_anchor = now
            main_transform = self.mem.read_u32(local_player + MAIN_CAMERA_TRANSFORM)
            if main_transform:
                main_pos = transform_get_position(self.mem, main_transform)
                if main_pos and vec3_ok(main_pos):
                    if v3_distance(main_pos, STATE.last_cam_pos) > 0.01:
                        STATE.cam_moved_tick = now
                    STATE.last_cam_pos = main_pos
                    STATE.local_main_camera = main_pos

    def _sample(self) -> None:
        if not self.mem.is_valid:
            return

        now = tick_ms()
        display_tick = int(getattr(STATE, "display_change_tick", 0) or 0)
        if display_tick > self._last_display_tick:
            self._last_display_tick = display_tick
            # Fullscreen / resolution switch: force a fresh page walk now, not
            # when the slower data thread happens to wake up.
            self.mem.clear_caches()
            self._last_local = 0
            self._last_anchor = 0
            self._last_hip = 0
            STATE.have_matrix = False
        match = STATE.current_match
        if not match:
            self._miss("no match")
            return

        # Read the pointer directly so a busy DataWorker cannot delay the
        # camera. While dead/spectating the game clears LocalPlayer, so fall
        # back to the current observer (same chain Data.cs uses).
        local_player = self.mem.read_u32(match + LOCAL_PLAYER)
        if not local_player:
            observer = self.mem.read_u32(match + CURRENT_OBSERVER)
            if observer:
                local_player = self.mem.read_u32(observer + OBSERVER_PLAYER)
        if not local_player:
            self._miss("no local player")
            return
        if local_player != self._last_local:
            self._last_local = local_player
            self._last_anchor = 0
            self._last_hip = 0

        self._anchor(local_player, now)

        follow_camera = self.mem.read_u32(local_player + FOLLOW_CAMERA)
        if not follow_camera:
            self._miss("FollowCamera")
            return

        camera = self.mem.read_u32(follow_camera + CAMERA)
        if not camera:
            self._miss("Camera")
            return

        camera_base_raw = self.mem.read_fresh(camera + 0x8, 4)
        if not camera_base_raw:
            self._miss("CameraBase")
            return
        camera_base = _struct.unpack_from("<I", camera_base_raw)[0]
        if not camera_base:
            self._miss("CameraBase=0")
            return

        matrix_data = self.mem.read_fresh(camera_base + VIEW_MATRIX, 64)
        if not matrix_data:
            self._miss("ViewMatrix read")
            return

        matrix = _struct.unpack_from("<16f", matrix_data)
        if not matrix_ok(matrix):
            self._miss("ViewMatrix invalid")
            return

        if matrix != STATE.camera_matrix:
            STATE.matrix_changes += 1
        STATE.camera_matrix = matrix
        STATE.matrix_tick = now
        STATE.have_matrix = True
        STATE.chain_fail = 0

        if not self._matrix_logged:
            self.log("[camera] ViewMatrix acquired! (local=0x%X)" % local_player)
            self._matrix_logged = True

    def run(self):
        _set_current_thread_priority(THREAD_PRIORITY_ABOVE_NORMAL)
        self.log("[camera] Camera thread started")
        try:
            self.mem.set_fast_mode(True)
        except Exception:
            pass
        time.sleep(0.2)

        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                self._sample()
            except Exception as exc:  # never kill the thread on a read hiccup
                self.log("[camera] sample error: %r" % exc)
                time.sleep(0.02)
            finally:
                STATE.cam_tick = tick_ms()
                self._samples += 1
                now = STATE.cam_tick
                if now - self._hz_tick >= 1000:
                    STATE.cam_hz = self._samples * 1000.0 / (now - self._hz_tick)
                    self._samples = 0
                    self._hz_tick = now

            elapsed = time.perf_counter() - started
            wait = CAMERA_INTERVAL - elapsed
            time.sleep(wait if wait > 0.0 else 0.001)


# ---------------------------------------------------------------------------
# AimbotAi.cs - aimbot visible + panel modes
# ---------------------------------------------------------------------------

_LEGIT_KEYS = {
    "left": 0x01, "right": 0x02, "middle": 0x04,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "space": 0x20,
    "x1": 0x05, "x2": 0x06,
}


def _vk_from_name(name: str) -> int:
    """Map a panel key name to a Win32 virtual-key code (default: right mouse)."""
    token = (name or "right").strip().lower()
    if token in _LEGIT_KEYS:
        return _LEGIT_KEYS[token]
    if len(token) == 1:
        if token.isalpha():
            return ord(token.upper())
        if token.isdigit():
            return 0x30 + int(token)
    if len(token) >= 2 and token[0] == "f" and token[1:].isdigit():
        number = int(token[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    return 0x02


def _key_down(name: str) -> bool:
    vk = _vk_from_name(name)
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class AimbotWorker(threading.Thread):
    """AimbotAi.Work port + DELAY / DRAG / LEGIT gating for the panel buttons.

    All four buttons use the same AIM core "aimbot visible" core: read the
    target's head collider pointer (entity+0x4E8) and write it into the
    shooting-collider slot (entity+0x54, "porul") ten times.
    """

    def __init__(self, mem: Memory, log: Optional[Callable[[str], None]] = None):
        super().__init__(name="sap-aim", daemon=True)
        self.mem = mem
        self.log = log or (lambda msg: None)
        self._stop = threading.Event()
        self._delay_target = 0
        self._delay_since = 0
        # DRAG MODE state: fire button must be held and the mouse must travel
        # at least CFG.drag_space pixels before the aimbot arms (same rule as
        # the original external aimbot engine).
        self._drag_down = False
        self._drag_accum = 0.0
        self._drag_prev = None
        self._drag_press_tick = 0
        global _CURRENT_AIMBOT_WORKER
        _CURRENT_AIMBOT_WORKER = self

    def stop(self):
        self._stop.set()

    def _drag_reset(self):
        self._drag_down = False
        self._drag_accum = 0.0
        self._drag_prev = None
        self._drag_start_pos = None
        self._drag_press_tick = 0
        global _CURRENT_AIMBOT_WORKER
        _CURRENT_AIMBOT_WORKER = self

    @staticmethod
    def _cursor_pos():
        pt = wintypes.POINT()
        try:
            user32.GetCursorPos(ctypes.byref(pt))
            return pt.x, pt.y
        except Exception:
            return 0, 0

    def run(self):
        _set_current_thread_priority(THREAD_PRIORITY_BELOW_NORMAL)
        try:
            self.mem.set_fast_mode(True)
        except Exception:
            pass
        while not self._stop.is_set():
            try:
                mode = CFG.aim_mode
                if mode == "off":
                    if self._drag_down:
                        self._drag_reset()
                    time.sleep(0.01)
                    continue
                if mode != "drag" and self._drag_down:
                    self._drag_reset()
                resize_age = tick_ms() - int(getattr(STATE, "geometry_change_tick", 0) or 0)
                if resize_age < AIM_RESIZE_SETTLE_MS:
                    # The overlay is hidden while the emulator resizes; aiming
                    # with the old/new mixed projection would be unreliable.
                    if self._drag_down:
                        self._drag_reset()
                    time.sleep(0.02)
                    continue
                if STATE.width <= 0 or STATE.height <= 0 or not STATE.have_matrix:
                    if self._drag_down:
                        self._drag_reset()
                    time.sleep(0.02)
                    continue
                # ESP tolerates a short matrix outage, but aiming must never use
                # an old projection (it would snap to a stale screen position).
                matrix_age = tick_ms() - STATE.matrix_tick if STATE.matrix_tick else 10 ** 9
                if matrix_age > 250:
                    if self._drag_down:
                        self._drag_reset()
                    time.sleep(0.02)
                    continue

                if mode == "delay":
                    radius = CFG.aimbot_fov
                    candidate = self._find_best_target(radius)
                    now = tick_ms()
                    if candidate is None:
                        self._delay_target = 0
                        time.sleep(0.004)
                        continue
                    if candidate.address != self._delay_target:
                        self._delay_target = candidate.address
                        self._delay_since = now
                        time.sleep(0.004)
                        continue
                    if now - self._delay_since < int(CFG.delay_ms):
                        time.sleep(0.004)
                        continue
                    self._aim_at_target(candidate)
                    time.sleep(AIM_INTERVAL)
                    continue

                if mode == "drag":
                    # Skill-based Upward Drag:
                    # Fire button must be held AND dragged UPWARDS on screen.
                    # Zero headshot on initial click! Only when upward drag >= CFG.drag_space does aim lock!
                    if not _key_down(CFG.trigger_key):
                        self._drag_reset()
                        time.sleep(0.004)
                        continue

                    pos = self._cursor_pos()
                    if not self._drag_down:
                        self._drag_down = True
                        self._drag_press_tick = tick_ms()
                        self._drag_accum = 0.0
                        self._drag_prev = pos
                        # Do NOT aim on first click! Wait for upward mouse drag!
                        time.sleep(0.004)
                        continue

                    # Accumulate upward mouse motion (dy > 0 means mouse moved UP on screen)
                    if self._drag_prev is not None:
                        dy = self._drag_prev[1] - pos[1]
                        if dy > 0:
                            self._drag_accum += dy
                    self._drag_prev = pos

                    # Threshold tied directly to DRAG SPACE slider (higher slider = must drag more)
                    threshold = max(8.0, float(CFG.drag_space or 35.0))

                    # Lock head ONLY when accumulated upward drag exceeds threshold!
                    if self._drag_accum >= threshold:
                        target = self._find_best_target(CFG.aimbot_fov)
                        if target is not None:
                            self._aim_at_target(target)

                    time.sleep(AIM_INTERVAL)
                    continue

                if mode == "legit":
                    held = _key_down(CFG.legit_key)
                    active = (not held) if CFG.legit_reverse else held
                    if not active:
                        time.sleep(0.004)
                        continue
                    radius = min(CFG.aimbot_fov, 200.0)
                    target = self._find_best_target(radius)
                    if target is not None:
                        self._aim_at_target(target)
                    time.sleep(LEGIT_INTERVAL)
                    continue

                # mode == "visible": plain AimbotAi behaviour (no key, 500 px)
                target = self._find_best_target(CFG.aimbot_fov)
                if target is not None:
                    self._aim_at_target(target)
                time.sleep(AIM_INTERVAL)
            except Exception as exc:
                self.log("[aim] error: %r" % exc)
                time.sleep(0.05)

    def _find_best_target(self, radius: float):
        best = None
        closest = float("inf")
        center_x = STATE.width / 2.0
        center_y = STATE.height / 2.0
        matrix = STATE.camera_matrix
        cam = STATE.local_main_camera
        max_distance = CFG.aim_max_distance
        now = tick_ms()

        for entity in STATE.entity_snapshot:
            if not entity.is_known or entity.is_team == "true":
                continue
            if entity.is_dead:
                continue
            if entity.head == (0.0, 0.0, 0.0):
                continue
            # Never aim at a position left over from before a resize/reset.
            if entity.last_data_ok_tick and now - entity.last_data_ok_tick > 1500:
                continue
            if CFG.ignore_knocked and entity.is_knocked:
                continue

            head2d = world_to_screen(matrix, entity.head, STATE.width, STATE.height)
            if head2d[0] < 1 or head2d[1] < 1:
                continue

            player_distance = v3_distance(cam, entity.head)
            if player_distance > max_distance:
                continue

            dx = head2d[0] - center_x
            dy = head2d[1] - center_y
            crosshair_distance = math.sqrt(dx * dx + dy * dy)
            if crosshair_distance < closest and crosshair_distance <= radius:
                closest = crosshair_distance
                best = entity

        return best

    def _aim_at_target(self, target: Entity):
        if target is None or target.address == 0:
            return
        head_collider = self.mem.read_u32(target.address + HEAD_COLLIDER)
        if not head_collider:
            return
        for _ in range(10):
            self.mem.write_u32(target.address + PORUL, head_collider)


    def _find_sniper_target(self, max_distance: float = 300.0):
        best = None
        closest = float("inf")
        center_x = STATE.width / 2.0
        center_y = STATE.height / 2.0
        matrix = STATE.camera_matrix
        cam = STATE.local_main_camera

        for entity in STATE.entity_snapshot:
            if not entity.is_known or entity.is_team == "true" or entity.is_dead:
                continue
            distance_3d = math.hypot(entity.root[0] - cam[0], entity.root[2] - cam[2])
            if distance_3d > max_distance:
                continue
            head2d = w2s(matrix, entity.head, STATE.width, STATE.height)
            if head2d is None:
                continue
            dx = head2d[0] - center_x
            dy = head2d[1] - center_y
            crosshair_distance = math.sqrt(dx * dx + dy * dy)
            if crosshair_distance < closest:
                closest = crosshair_distance
                best = entity
        return best

    def _aim_at_target_body(self, target: Entity):
        if target is None or target.address == 0:
            return
        head_collider = self.mem.read_u32(target.address + HEAD_COLLIDER)
        if not head_collider:
            return
        for _ in range(12):
            self.mem.write_u32(target.address + PORUL, head_collider)

_CURRENT_AIMBOT_WORKER = None

def get_sniper_target_and_lock(max_distance: float = 300.0):
    global _CURRENT_AIMBOT_WORKER
    if _CURRENT_AIMBOT_WORKER is not None:
        target = _CURRENT_AIMBOT_WORKER._find_sniper_target(max_distance)
        if target:
            _CURRENT_AIMBOT_WORKER._aim_at_target_body(target)
            return True
    return False
