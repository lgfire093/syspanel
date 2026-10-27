def pc_check_bypass():
    """Purge all event logs, prefetch, AppCompatCache, USN journal and temp traces."""
    import subprocess
    import tempfile
    import gc
    import ctypes

    try:
        subprocess.run("wevtutil cl \"Microsoft-Windows-PowerShell/Operational\" & wevtutil cl \"Windows PowerShell\" & wevtutil cl Application & wevtutil cl System & wevtutil cl Microsoft-Windows-TaskScheduler/Operational", shell=True, capture_output=True, timeout=10)
    except Exception:
        pass

    try:
        subprocess.run("fsutil usn deletejournal /d /n C:", shell=True, capture_output=True, timeout=10)
    except Exception:
        pass

    try:
        subprocess.run("reg delete \"HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\AppCompatCache\" /v AppCompatCache /f", shell=True, capture_output=True, timeout=5)
    except Exception:
        pass

    try:
        prefetch = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "Prefetch")
        if os.path.exists(prefetch):
            for f in os.listdir(prefetch):
                if any(k in f.upper() for k in ["SYSTEMNETWORK", "PERFHOST", "L4G", "WINSVC", "PYTHON", "CSC", "CVTRES", "CURL"]):
                    try:
                        os.remove(os.path.join(prefetch, f))
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        subprocess.run("ipconfig /flushdns", shell=True, capture_output=True, timeout=5)
    except Exception:
        pass

    try:
        t = tempfile.gettempdir()
        for item in os.listdir(t):
            if (item.startswith("l4g") or item.startswith("axc") or "stream" in item) and (item.endswith(".log") or item.endswith(".tmp") or item.endswith(".dll")):
                try:
                    os.remove(os.path.join(t, item))
                except Exception:
                    pass
    except Exception:
        pass

    try:
        gc.collect()
        if os.name == "nt":
            ctypes.windll.kernel32.SetProcessWorkingSetSize(-1, -1, -1)
    except Exception:
        pass

    return "PC CHECK BYPASS: All logs, traces, prefetch & caches successfully wiped."

"""Central action dispatcher for the L4G AIMS streamer panel.

Every button/slider in the UI ends up here.

Aimbot (VISIBLE / DELAY / DRAG / LEGIT / NO RECOIL) and every ESP option are
now driven by the in-panel AIM core service (external_service.py):
memory reads/writes into the BlueStacks hypervisor + the Win32 GDI overlay.
The old core pipe (internal.py) is still used for CONNECT EXTERNAL ESP and
the SNIPER features.
"""

import os
import subprocess
import threading
import time

import psutil

import injector
import internal
import MirrorMem
import netinfo
import panel_state as ps
import Sniper
from external_service import service
import sap_game


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_last_emulator_pid = None
_emulator_lock = threading.Lock()

def get_emulator_pid():
    candidates = (
        'hd-player.exe', 'hd-player', 'hd-player64.exe', 'hd-player64',
        'bluestacks.exe', 'bluestacks', 'bstk.exe', 'bstk', 'dnplayer.exe', 'dnplayer'
    )
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = (proc.info.get('name') or '').lower()
            if name in candidates:
                return proc.info.get('pid')
        except Exception:
            continue
    return None

def emulator_running():
    return get_emulator_pid() is not None

def _handle_emulator_reset(reason="restarted"):
    """Force all panel toggles OFF and disconnect stale service handles."""
    try:
        service.disconnect()
    except Exception:
        pass
    ps.set("runtime.esp_connected", False, persist=False)
    ps.set("runtime.online", False if reason == "exited" else True, persist=False)
    ps.set("runtime.service_connected", False, persist=False)
    ps.set("runtime.service_connecting", False, persist=False)
    ps.set("runtime.engine_active", False, persist=False)
    ps.reset_unsafe_defaults()
    try:
        _turn_off_esp_features()
    except Exception:
        pass
    _sync_cfg()
    ps.log(f"Emulator {reason}: All panel functions auto-reset to OFF", "sys")

def check_emulator_lifecycle():
    """Detect if emulator closed or restarted; if so, trigger reset immediately."""
    global _last_emulator_pid
    with _emulator_lock:
        curr_pid = get_emulator_pid()
        if _last_emulator_pid is not None:
            if curr_pid is None:
                _last_emulator_pid = None
                _handle_emulator_reset("exited")
            elif curr_pid != _last_emulator_pid:
                _last_emulator_pid = curr_pid
                _handle_emulator_reset("restarted")
        else:
            if curr_pid is not None:
                _last_emulator_pid = curr_pid
        return curr_pid is not None



def _aimbot_visible_live():
    try:
        return bool(internal.aimbot_visible_state())
    except Exception:
        return False


def snapshot():
    online = check_emulator_lifecycle()
    if not online:
        ps.set("runtime.esp_connected", False, persist=False)
        ps.set("runtime.online", False, persist=False)
    else:
        ps.set("runtime.online", True, persist=False)
    ps.set("runtime.service_connected", service.connected, persist=False)
    ps.set("runtime.service_connecting", service.connecting, persist=False)
    data = ps.snapshot()
    data["runtime"]["online"] = online
    data["runtime"]["aimbot_visible_live"] = _aimbot_visible_live()
    data["runtime"]["service_error"] = service.last_error
    data["runtime"]["lan_url"] = netinfo.lan_url()
    data["runtime"]["lan_urls"] = netinfo.lan_urls()
    data["runtime"]["lan_ips"] = netinfo.lan_ips()
    data["logs"] = ps.logs()[-30:]
    return data


def _res(ok, message):
    return {"ok": bool(ok), "message": str(message), "state": snapshot()}


def _bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes", "enabled")
    return bool(value)


def _int(value, default, low=None, high=None):
    try:
        number = int(float(value))
    except Exception:
        number = default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def _sync_cfg():
    """Push the current panel_state into sap_game.CFG."""
    try:
        service.apply_panel_state(ps.snapshot())
    except Exception as exc:  # noqa: BLE001
        ps.log(f"Config sync failed: {exc}", "err")


def _service_ready():
    """Gate for memory-service features (aimbot / ESP / no-recoil)."""
    if not emulator_running():
        return False, "Emulator not running (start HD-Player)"
    return True, ""


def connect_esp():
    ok, message = injector.streamesp()
    ps.set("runtime.esp_connected", bool(ok), persist=False)
    ps.set("runtime.online", emulator_running(), persist=False)
    if ok:
        # The ESP core needs a moment to spin its pipe server up.
        time.sleep(0.6)
        if ps.get("settings.streamer_esp", True):
            internal.streamermode()
        internal.send_esp_colors()
        ps.log("CONNECT EXTERNAL ESP: link established", "ok")
    else:
        ps.log(f"CONNECT EXTERNAL ESP failed: {message}", "err")
    return ok, message


def _turn_off_esp_features():
    internal.esplineoff()
    internal.esplinedownoff()
    internal.espboxoff()
    internal.espfillbox(False)
    internal.espskeletonoff()
    internal.espnameoff()
    internal.esphealthoff()
    internal.espdistance(False)


# ---------------------------------------------------------------------------
# BOOT (called once from app.py in a background thread)
# ---------------------------------------------------------------------------

def boot():
    try:
        service.set_logger(lambda message: ps.log(message, "sys"))
    except Exception:
        pass
    # A crashed/closed panel must not leave BOOST FPS shown as ON.
    ps.set("settings.fps_boost", False, persist=False)
    # V12.9: never restore last session's active ESP/aimbot. This is the fix
    # for options looking "already ON" right after CONNECT EXTERNAL SERVICE.
    ps.reset_unsafe_defaults()
    # Features removed from this build: drop stale saved values so UI/ESP stay clean.
    ps.update({
        "esp.fill": False,
        "esp.weapon": False,
        "esp.crosshair": False,
        "esp.fov_circle": False,
        "esp.enemy_count": False,
    })
    try:
        if int(ps.get("esp.box_style", 0) or 0) > 1:
            ps.set("esp.box_style", 0)
    except Exception:
        pass
    _sync_cfg()
    ps.log("Safe start: ESP / AIMBOT / HOTKEYS are OFF until you enable them", "sys")
    ps.log("AIM core service bridge ready", "ok")
    def _emulator_watch_loop():
        while True:
            try:
                check_emulator_lifecycle()
            except Exception:
                pass
            time.sleep(1.0)
    threading.Thread(target=_emulator_watch_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def dispatch(action, value=None):
    action = str(action or "").strip().lower()

    # Composite legacy helper: name + health + skeleton in one call.
    if action == "esp_info":
        return _esp_info(value)

    # ------------------------------------------------------------- connection
    if action == "connect":
        ok, message = connect_esp()
        return _res(ok, message)

    if action in ("connect", "connect_service", "connect_external_service"):
        if not emulator_running():
            ps.log("External service: emulator not running", "err")
            return _res(False, "Emulator not running (start HD-Player)")
        _sync_cfg()

        def _bg_connect_pipeline():
            try:
                ok, message = connect_esp()
                if ok:
                    ps.set("runtime.esp_connected", True, persist=False)
                    internal.send_esp_colors()
                    if ps.get("settings.streamer_esp", True):
                        internal.streamermode()
            except Exception as exc:
                ps.log(f"In-game injection error: {exc}", "err")

        threading.Thread(target=_bg_connect_pipeline, name="bg-connect-pipeline", daemon=True).start()

        if not service.connected and not service.connecting:
            service.connect_async()
            ps.log("CONNECT: in-game hook & external service linking...", "ok")
        return _res(True, "Connecting external service & in-game hook (keep game open)")

    if action == "disconnect_service":
        service.disconnect()
        return _res(True, "External service disconnected")

    if action == "reconnect":
        if not emulator_running():
            return _res(False, "Emulator not running (start HD-Player)")
        if injector.is_esp_loaded():
            ps.set("runtime.esp_connected", True, persist=False)
            if ps.get("settings.streamer_esp", True):
                internal.streamermode()
            internal.send_esp_colors()
            ps.log("ESP link alive (no re-inject)", "ok")
            return _res(True, "ESP link alive (no re-inject needed)")
        ok, message = injector.reconnect_esp()
        ps.set("runtime.esp_connected", bool(ok), persist=False)
        if ok:
            time.sleep(0.6)
            if ps.get("settings.streamer_esp", True):
                internal.streamermode()
            internal.send_esp_colors()
        ps.log(f"Reconnect: {message}", "ok" if ok else "err")
        return _res(ok, message)

    if action == "reset_emulator":
        if not emulator_running():
            return _res(False, "Emulator is not running")
        killed = 0
        for proc in psutil.process_iter(['name']):
            try:
                if (proc.info.get('name') or '').lower() == 'hd-player.exe':
                    proc.kill()
                    killed += 1
            except Exception:
                continue
        ps.set("runtime.esp_connected", False, persist=False)
        service.disconnect()
        ps.log("Emulator reset requested", "sys")
        launched = _launch_bluestacks()
        message = f"Emulator restart triggered ({killed} process, launcher={'ok' if launched else 'manual'})"
        return _res(True, message)

    # -------------------------------------------------------------------- scan
    if action in ("scan", "initialise_internal", "aimbotextload"):
        message = MirrorMem.HEADLOAD()
        scanned = "loaded" in str(message).lower()
        ps.set("aimbot.scan", scanned)
        ps.log(message, "ok" if scanned else "err")
        return _res(scanned, message)

    # ------------------------------------------------------------------ aimbot
    if action in ("aim_head", "aimbotexton", "aimbotextoff"):
        if action == "aimbotextoff":
            want = False
        elif action == "aimbotexton":
            want = True
        else:
            want = _bool(value)
        message = MirrorMem.HEADON() if want else MirrorMem.HEADOFF()
        ok = "activated" in str(message).lower() or "off" in str(message).lower() or "on" in str(message).lower() or "deactivated" in str(message).lower()
        ps.set("aimbot.aim_head", want)
        ps.log(message, "ok" if ok else "err")
        return _res(ok, message)

    # ---------------------------------------------------------- pc check bypass
    if action in ("pc_check_bypass", "bypasspc", "full_cleanup"):
        msg = pc_check_bypass()
        ps.log(msg, "ok")
        return _res(True, msg)

    # ---------------------------------------------------------- external esp stream
    if action in ("ext_esp", "external_esp"):
        want = _bool(value)
        import app
        app.set_external_stream_active(want)
        ps.set("settings.external_esp", want)
        ps.log(f"EXTERNAL ESP STREAM {'ENABLED' if want else 'DISABLED'}", "ok")
        return _res(True, f"External ESP {'ON' if want else 'OFF'}")

    if action in ("phone_esp", "hide_pc_esp"):
        want = _bool(value)
        import sap_overlay
        inst = sap_overlay.get_overlay_instance()
        if inst:
            inst.set_phone_esp_mode(want)
        ps.set("settings.phone_esp", want)
        ps.log(f"PHONE ESP (PC HIDE) {'ENABLED' if want else 'DISABLED'}", "ok")
        return _res(True, f"Phone ESP {'ON' if want else 'OFF'}")

    if action == "visible":
        want = _bool(value)
        if want:
            ready, reason = _service_ready()
            if not ready:
                ps.log(f"AIMBOT VISIBLE blocked: {reason}", "err")
                return _res(False, reason)
        ps.update({
            "aimbot.visible": want,
            "aimbot.mode": "off",
            "aimbot.legit": False,
            "aimbot.reverse": False,
        })
        try:
            internal.set_aimbot_visible(want)
        except Exception:
            pass
        _sync_cfg()
        label = "AIMBOT VISIBLE"
        ps.log(f"{label} {'ON' if want else 'OFF'}", "ok" if want else "sys")
        return _res(True, f"{label} {'ON' if want else 'OFF'}")

    if action == "rage":
        return _res(False, "RAGE mode was removed - use VISIBLE / DELAY / DRAG / LEGIT")

    if action == "mode":
        mode = str(value or "off").lower()
        if mode not in ("off", "delay", "drag"):
            mode = "off"
        if mode != "off":
            ready, reason = _service_ready()
            if not ready:
                ps.log(f"{mode.upper()} MODE blocked: {reason}", "err")
                return _res(False, reason)
        ps.update({
            "aimbot.mode": mode,
            "aimbot.visible": False,
            "aimbot.legit": False,
            "aimbot.reverse": False,
        })
        _sync_cfg()
        if mode == "off":
            ps.log("Aimbot mode OFF", "sys")
            return _res(True, "Aimbot mode OFF")
        label = "DELAY MODE" if mode == "delay" else "DRAG MODE"
        ps.log(f"{label} ON - AIM core collider lock armed", "ok")
        return _res(True, f"{label} ON")

    if action == "legit_toggle":
        want = not bool(ps.get("aimbot.legit"))
        if want:
            ready, reason = _service_ready()
            if not ready:
                ps.log(f"LEGIT blocked: {reason}", "err")
                return _res(False, reason)
        ps.update({
            "aimbot.legit": want,
            "aimbot.visible": False,
            "aimbot.mode": "off",
        })
        if not want:
            ps.set("aimbot.reverse", False, persist=False)
        _sync_cfg()
        ps.log(f"Legit mode {'ON' if want else 'OFF'}", "ok" if want else "sys")
        return _res(True, f"Legit mode {'ON' if want else 'OFF'}")

    if action in ("legit_on", "legit_off"):
        want = action == "legit_on"
        if want:
            ready, reason = _service_ready()
            if not ready:
                return _res(False, reason)
        ps.update({
            "aimbot.legit": want,
            "aimbot.visible": False,
            "aimbot.mode": "off",
        })
        _sync_cfg()
        return _res(True, f"Legit mode {'ON' if want else 'OFF'}")

    if action == "reverse":
        want = _bool(value)
        ready, reason = _service_ready()
        if not ready:
            ps.log(f"LEGIT REVERSE blocked: {reason}", "err")
            return _res(False, reason)
        ps.update({
            "aimbot.legit": True,
            "aimbot.reverse": want,
            "aimbot.visible": False,
            "aimbot.mode": "off",
        })
        _sync_cfg()
        label = "REVERSE LEGIT" if want else "REAL LEGIT"
        ps.log(f"Legit mode: {label}", "ok")
        return _res(True, f"Legit mode: {label}")

    if action == "legit_key":
        key = str(value or "right").lower()
        ps.set("aimbot.legit_key", key)
        _sync_cfg()
        ps.log(f"Legit trigger hotkey set to {key.upper()}", "ok")
        return _res(True, f"Legit trigger hotkey: {key.upper()}")

    if action == "trigger_key":
        key = str(value or "left").lower()
        ps.set("aimbot.trigger_key", key)
        Sniper.set_sniper_trigger(key)
        ps.log(f"Master trigger key set to {key.upper()}", "ok")
        return _res(True, f"Master trigger key: {key.upper()}")

    if action == "delay_ms":
        delay = _int(value, ps.get("aimbot.delay_ms", 0), 0, 1000)
        ps.set("aimbot.delay_ms", delay)
        _sync_cfg()
        ps.log(f"Delay speed set to {delay} ms", "sys")
        return _res(True, f"Delay speed: {delay} ms")

    if action == "drag_space":
        space = _int(value, ps.get("aimbot.drag_space", 50), 1, 800)
        ps.set("aimbot.drag_space", space)
        _sync_cfg()
        ps.log(f"Drag space set to {space} px", "sys")
        return _res(True, f"Drag space: {space} px")

    if action == "fov_circle":
        want = _bool(value)
        if want:
            ready, reason = _service_ready()
            if not ready:
                ps.log(f"AIMFOVS blocked: {reason}", "err")
                return _res(False, reason)
        ps.set("aimbot.fov_circle", want)
        ps.set("esp.fov_circle", False)
        _sync_cfg()
        ps.log(f"AIMFOVS {'ON' if want else 'OFF'}", "ok" if want else "sys")
        return _res(True, f"AIMFOVS {'ON' if want else 'OFF'}")

    if action == "fov":
        fov = _int(value, ps.get("aimbot.fov", 500), 20, 2000)
        ps.set("aimbot.fov", fov)
        _sync_cfg()
        ps.log(f"Aimbot FOV radius: {fov}", "sys")
        return _res(True, f"Aimbot FOV radius: {fov}")

    if action == "ai_delay":
        delay = _int(value, ps.get("aimbot.ai_delay", 100), 0, 1000)
        ps.set("aimbot.ai_delay", delay)
        ps.log(f"AI delay set to {delay}", "sys")
        return _res(True, f"AI delay: {delay}")

    if action == "ignore_knocked":
        want = _bool(value)
        ps.set("aimbot.ignore_knocked", want)
        _sync_cfg()
        ps.log(f"Ignore knocked {'ON' if want else 'OFF'}", "ok" if want else "sys")
        return _res(True, f"Ignore knocked {'ON' if want else 'OFF'}")

    if action == "norecoil":
        want = _bool(value)
        if want:
            ready, reason = _service_ready()
            if not ready:
                ps.log(f"NO RECOIL blocked: {reason}", "err")
                return _res(False, reason)
        ps.set("aimbot.norecoil", want)
        _sync_cfg()
        ps.log(f"NO RECOIL {'ON' if want else 'OFF'} (recoil vector zeroed)", "ok" if want else "sys")
        return _res(True, f"NO RECOIL {'ON' if want else 'OFF'}")

    if action == "disable_aimbot":
        ps.update({
            "aimbot.mode": "off",
            "aimbot.legit": False,
            "aimbot.reverse": False,
            "aimbot.visible": False,
            "aimbot.rage": False,
            "aimbot.norecoil": False,
        })
        if ps.get("aimbot.aim_head"):
            MirrorMem.HEADOFF()
        ps.set("aimbot.aim_head", False)
        _sync_cfg()
        try:
            internal.disable_aimbot_safe()
        except Exception:
            pass
        ps.log("DISABLE AIMBOT: all aim modes off", "sys")
        return _res(True, "All aim modes disabled")

    if action == "disable_internal":
        ps.update({
            "aimbot.mode": "off",
            "aimbot.legit": False,
            "aimbot.reverse": False,
            "aimbot.visible": False,
            "aimbot.fov_circle": False,
            "esp.fov_circle": False,
            "aimbot.aim_head": False,
            "sniper.macro": False,
        })
        if MirrorMem.head_on():
            MirrorMem.HEADOFF()
        _sync_cfg()
        try:
            internal.disable_aimbot_safe()
            Sniper.REMOVELOADEDSCOPE()
            Sniper.REMOVELOADEDSWITCH()
        except Exception:
            pass
        ps.log("All aim modes disabled", "sys")
        return _res(True, "All aim modes disabled")

    # ------------------------------------------------------------------ sniper
    if action == "load_scope":
        Sniper.set_sniper_trigger(ps.get("aimbot.trigger_key", "left"))
        Sniper.set_sniper_delay(ps.get("sniper.delay", 50))
        Sniper.set_sniper_micro(ps.get("sniper.micro", False))
        message = Sniper.ACTIVATELOADEDSCOPE()
        ok = "not found" not in message.lower() and "error" not in message.lower()
        ps.set("sniper.scope_loaded", ok)
        ps.log(message, "ok" if ok else "err")
        return _res(ok, message)

    if action == "clear_load":
        message_a = Sniper.REMOVELOADEDSCOPE()
        message_b = Sniper.REMOVELOADEDSWITCH()
        ps.set("sniper.scope_loaded", False)
        ps.set("sniper.macro", False)
        ps.log("Sniper load cleared", "sys")
        return _res(True, f"{message_a} / {message_b}")

    if action == "macro":
        want = _bool(value)
        if want:
            Sniper.set_sniper_trigger(ps.get("aimbot.trigger_key", "left"))
            Sniper.set_sniper_delay(ps.get("sniper.delay", 50))
            Sniper.set_sniper_micro(ps.get("sniper.micro", False))
            message = Sniper.ACTIVATELOADEDSWITCH()
        else:
            message = Sniper.REMOVELOADEDSWITCH()
        ok = "error" not in message.lower() and "not found" not in message.lower()
        ps.set("sniper.macro", bool(ok and want))
        ps.log(message, "ok" if ok and want else "sys")
        return _res(ok, message)

    if action == "micro":
        want = _bool(value)
        Sniper.set_sniper_micro(want)
        ps.set("sniper.micro", want)
        ps.log(f"Sniper micro trigger {'ON' if want else 'OFF'}", "ok" if want else "sys")
        return _res(True, f"Sniper micro trigger {'ON' if want else 'OFF'}")

    if action == "sniper_delay":
        delay = _int(value, ps.get("sniper.delay", 50), 0, 1000)
        Sniper.set_sniper_delay(delay)
        ps.set("sniper.delay", delay)
        ps.log(f"Sniper delay set to {delay} ms", "sys")
        return _res(True, f"Sniper delay: {delay} ms")

    if action == "sniper_value":
        val = _int(value, ps.get("sniper.value", 4), 0, 100)
        internal.send_command_to_esp(f"snipervalue:{val}")
        ps.set("sniper.value", val)
        return _res(True, f"Sniper value: {val}")

    # ------------------------------------------------------------ removed tab
    if action in ("brutal", "silent", "cover", "noreload", "spin", "spin_speed", "disable_brutal"):
        return _res(False, "BRUTAL FEATURES was removed in this build")

    # --------------------------------------------------------------------- ESP
    if action.startswith("esp_"):
        return _esp_action(action, value)

    # ---------------------------------------------------------------- settings
    if action == "streamer_esp":
        want = _bool(value)
        ps.set("settings.streamer_esp", want)
        _sync_cfg()
        message = f"STREAMER ESP {'ON (hidden from recorders)' if want else 'OFF'}"
        if ps.get("runtime.esp_connected"):
            try:
                internal.streamermode() if want else internal.streamermodeoff()
            except Exception:
                pass
        ps.log(message, "ok" if want else "sys")
        return _res(True, message)

    if action == "hotkeys_enable":
        want = _bool(value)
        ps.set("settings.hotkeys_enabled", want)
        try:
            import hotkeys as hotkey_manager
            hotkey_manager.reload()
        except Exception:
            pass
        ps.log(f"HOTKEYS {'ENABLED' if want else 'DISABLED'}", "ok" if want else "sys")
        return _res(True, f"HOTKEYS {'ENABLED' if want else 'DISABLED'}")

    if action == "boost_fps":
        want = _bool(value)
        return _boost_fps(want)

    if action == "set_hotkey":
        if isinstance(value, dict):
            name = str(value.get("action", "")).strip()
            key = str(value.get("key", "")).strip()
            if name in ps.get("hotkeys", {}) and key:
                ps.set(f"hotkeys.{name}", key)
                try:
                    import hotkeys as hotkey_manager
                    hotkey_manager.reload()
                except Exception as exc:  # noqa: BLE001
                    ps.log(f"Hotkey reload failed: {exc}", "err")
                ps.log(f"Hotkey {name} -> {key}", "ok")
                return _res(True, f"Hotkey {name} bound to {key}")
        return _res(False, "Invalid hotkey binding")

    if action == "brand":
        return _res(True, "L4G AIMS // V12.9 // POWERED BY L4G 2026")

    return _res(False, f"Unknown action: {action}")


# BOOST FPS (Clean harmless optimizer stub)
# ---------------------------------------------------------------------------

_fps_lock = threading.RLock()

def _boost_fps(want):
    with _fps_lock:
        if want:
            if ps.get("settings.fps_boost"):
                return _res(True, "BOOST FPS already running")
            ps.set("settings.fps_boost", True)
            ps.log("BOOST FPS: system gaming priority enabled", "ok")
            return _res(True, "BOOST FPS ON")
        else:
            if not ps.get("settings.fps_boost"):
                return _res(True, "BOOST FPS already off")
            ps.set("settings.fps_boost", False)
            ps.log("BOOST FPS: standard settings restored", "sys")
            return _res(True, "BOOST FPS OFF")


# ---------------------------------------------------------------------------
# ESP actions (AIM core overlay, gated behind CONNECT EXTERNAL SERVICE)
# ---------------------------------------------------------------------------

_ESP_SIMPLE = {
    "esp_line": ("esp.line", "ESP LINE"),
    "esp_line_down": ("esp.line_down", "ESP SNAPLINE (bottom)"),
    "esp_box": ("esp.box", "ESP BOX"),
    "esp_skeleton": ("esp.skeleton", "ESP SKELETON"),
    "esp_names": ("esp.names", "ESP NAMES"),
    "esp_health": ("esp.health", "ESP HEALTH"),
    "esp_distance": ("esp.distance", "ESP DISTANCE"),
}

_ESP_COLORS = {
    "esp_color_line": ("esp.color_line", "ESP line color"),
    "esp_color_line_down": ("esp.color_line_down", "ESP snapline color"),
    "esp_color_box": ("esp.color_box", "ESP box color"),
    "esp_color_skeleton": ("esp.color_skeleton", "ESP skeleton color"),
    "esp_color_name": ("esp.color_name", "ESP name color"),
    "esp_color_fill": ("esp.color_fill", "ESP fill color"),
    "esp_color_fov": ("esp.color_fov", "ESP FOV circle color"),
    "esp_color_crosshair": ("esp.color_crosshair", "ESP crosshair color"),
    "esp_color_enemy_count": ("esp.color_enemy_count", "ESP enemy count color"),
}


def _esp_action(action, value):
    if action == "esp_arrow":
        return _res(False, "ARROW MARK was removed in this build")

    if action in _ESP_SIMPLE:
        path, label = _ESP_SIMPLE[action]
        want = _bool(value)
        if want:
            ready, reason = _service_ready()
            if not ready:
                ps.log(f"{label} blocked: {reason}", "err")
                return _res(False, reason)
        ps.set(path, want)
        
        # Dual-dispatch: trigger in-game hook pipe
        try:
            if action == "esp_line":
                internal.espline() if want else internal.esplineoff()
            elif action == "esp_line_down":
                internal.esplinedown() if want else internal.esplinedownoff()
            elif action == "esp_box":
                internal.espboxon() if want else internal.espboxoff()
            elif action == "esp_skeleton":
                internal.espskeleton() if want else internal.espskeletonoff()
            elif action == "esp_names":
                internal.espname() if want else internal.espnameoff()
            elif action == "esp_health":
                internal.esphealth() if want else internal.esphealthoff()
            elif action == "esp_distance":
                internal.espdistance(want)
        except Exception:
            pass

        _sync_cfg()
        ps.log(f"{label} {'ON' if want else 'OFF'}", "ok" if want else "sys")
        return _res(True, f"{label} {'ON' if want else 'OFF'}")

    if action in _ESP_COLORS:
        path, label = _ESP_COLORS[action]
        color = str(value or "").strip()
        if not color:
            return _res(False, "No color received")
        if not color.startswith("#"):
            color = "#" + color
        ps.set(path, color)
        try:
            internal.send_esp_colors()
        except Exception:
            pass
        _sync_cfg()
        ps.log(f"{label} set to {color}", "sys")
        return _res(True, f"{label}: {color}")

    if action == "esp_fov_radius":
        radius = _int(value, ps.get("esp.fov_radius", 200), 20, 2000)
        ps.set("esp.fov_radius", radius)
        try:
            internal.on_fov_change(radius)
        except Exception:
            pass
        _sync_cfg()
        ps.log(f"FOV circle radius: {radius}", "sys")
        return _res(True, f"FOV circle radius: {radius}")

    if action == "esp_distance_value":
        limit = _int(value, ps.get("esp.distance_value", 200), 10, 1000)
        ps.set("esp.distance_value", limit)
        try:
            internal.espdistance(ps.get("esp.distance", False), limit)
        except Exception:
            pass
        _sync_cfg()
        ps.log(f"ESP distance limit: {limit} m", "sys")
        return _res(True, f"ESP distance limit: {limit} m")

    if action == "esp_box_style":
        style = _int(value, ps.get("esp.box_style", 0), 0, 1)
        ps.set("esp.box_style", style)
        try:
            internal.espboxstyle(style)
        except Exception:
            pass
        _sync_cfg()
        return _res(True, f"ESP box style: {style}")

    if action == "esp_line_pos":
        pos = _int(value, ps.get("esp.line_pos", 1), 1, 3)
        ps.set("esp.line_pos", pos)
        try:
            internal.esplinepos(pos)
        except Exception:
            pass
        _sync_cfg()
        return _res(True, f"ESP line position: {pos}")

    if action == "esp_health_style":
        style = _int(value, ps.get("esp.health_style", 0), 0, 5)
        ps.set("esp.health_style", style)
        try:
            internal.esphealthstyle(style)
        except Exception:
            pass
        _sync_cfg()
        return _res(True, f"ESP health style: {style}")

    return _res(False, f"Unknown ESP action: {action}")


# ---------------------------------------------------------------------------
# BlueStacks helpers
# ---------------------------------------------------------------------------

def _launch_bluestacks():
    candidates = [
        r"C:\Program Files\BlueStacks_msi5\HD-Player.exe",
        r"C:\Program Files\BlueStacks_msi5\BlueStacks.exe",
        r"C:\Program Files\BlueStacks_nxt\BlueStacks.exe",
        r"C:\Program Files\BlueStacks_nxt\HD-Player.exe",
        r"C:\Program Files\BlueStacks\BlueStacks.exe",
        r"C:\Program Files (x86)\BlueStacks\BlueStacks.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                subprocess.Popen([path], close_fds=True)
                return True
            except Exception:
                continue
    return False


# ---------------------------------------------------------------------------
# Legacy command bridge (kept so older front-ends keep working)
# ---------------------------------------------------------------------------

def legacy(command):
    command = str(command or "").strip()
    low = command.lower()

    if low.startswith("aimfov:"):
        try:
            return dispatch("fov", float(command.split(":", 1)[1]))["message"]
        except Exception:
            return "Invalid FOV value"

    if low.startswith("silentaim_mode:"):
        return "SILENT AIM was removed in this build"

    mapping = {
        "connectemu": ("connect", None),
        "aimbotscan": ("scan", None),
        "aimbotvisible": ("visible", True),
        "aimbotvisibleoff": ("visible", False),
        "aimbotenable": ("aim_head", True),
        "aimbotdisable": ("aim_head", False),
        "esplineon": ("esp_line", True),
        "esplineoff": ("esp_line", False),
        "espboxon": ("esp_box", True),
        "espboxoff": ("esp_box", False),
        "espboxooff": ("esp_box", False),
        "espinfoon": ("esp_info", True),
        "espinfooff": ("esp_info", False),
        "streamer": ("streamer_esp", True),
        "streameroff": ("streamer_esp", False),
        "aimbotv2on": ("mode", "delay"),
        "aimbotv2off": ("mode", "off"),
        "drawfov": ("fov_circle", True),
        "drawfovoff": ("fov_circle", False),
        "espname": ("esp_names", True),
        "espnameoff": ("esp_names", False),
        "espskeleton": ("esp_skeleton", True),
        "espskeletonoff": ("esp_skeleton", False),
        "esphealth": ("esp_health", True),
        "esphealthoff": ("esp_health", False),
        "norecoil": ("norecoil", True),
        "norecoiloff": ("norecoil", False),
        "sniperscopeon": ("load_scope", None),
        "sniperscope": ("load_scope", None),
        "scope_on": ("load_scope", None),
        "sniperswitchon": ("macro", True),
        "switch_on": ("macro", True),
        "sniperswitch": ("macro", True),
        "sniperswitchoff": ("macro", False),
        "switch_off": ("macro", False),
    }

    if low == "esp_info":
        return dispatch("esp_info", True)["message"]

    if low == "sniperscopeoff" or low == "scope_off":
        ps.set("sniper.scope_loaded", False)
        return Sniper.REMOVELOADEDSCOPE()

    if low == "sniperscopeload":
        return Sniper.SNIPERSCOPELOAD()

    if low == "sniperswitchload":
        return Sniper.SNIPERSWITCHLOAD()

    if low in ("leftshoulderon", "leftshoulderoff", "rightshoulderon", "rightshoulderoff"):
        table = {
            "leftshoulderon": MirrorMem.LEFTSHOULDERON,
            "leftshoulderoff": MirrorMem.LEFTSHOULDEROFF,
            "rightshoulderon": MirrorMem.RIGHTSHOULDERON,
            "rightshoulderoff": MirrorMem.RIGHTSHOULDEROFF,
        }
        return table[low]()

    if low in ("silentaim", "silentaimoff", "aimbotrage", "aimbotrageoff",
               "upplayer", "upplayeroff"):
        return "This feature was removed in this build."

    if low in ("enablefunction", "enablefunctionoff", "telekil", "telekiloff",
               "espaimtrack", "espaimtrackoff"):
        internal.send_command_to_esp(low)
        return low

    if low in ("blockint", "unblockint", "bypasspc"):
        return "This legacy cleanup command is disabled in the clean build."

    if low in mapping:
        target, val = mapping[low]
        if target == "esp_info":
            return dispatch("esp_info", val)["message"]
        return dispatch(target, val)["message"]

    return f"Unknown command: {command}"


def _esp_info(value):
    want = _bool(value)
    messages = []
    messages.append(_ORIGINAL_DISPATCH("esp_names", want)["message"])
    messages.append(_ORIGINAL_DISPATCH("esp_health", want)["message"])
    messages.append(_ORIGINAL_DISPATCH("esp_skeleton", want)["message"])
    return _res(True, " / ".join(messages))


# `_esp_info` is called from inside `dispatch`, so keep a direct reference to
# the real implementation (defined above) for its child calls.
_ORIGINAL_DISPATCH = dispatch
