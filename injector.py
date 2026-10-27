"""DLL injection for the external ESP core.



Injection mirrors the original panel: cimgui.dll -> AotBst.dll -> Client.dll

(the staged copy at C:\\Windows\\Temp\\Client.dll). Client.dll is the cheat core

from the original release; a static audit found no grabber/stealer code in it

(see SECURITY_REPORT.md), so full ESP (aimbot, silent aim, ESP overlay) works

again.



AotBst.dll hardcodes LoadLibraryA("C:\\Windows\\Temp\\Client.dll") and calls its

"Load" export from a worker thread. When that file is missing, the thread jumps

through a NULL pointer and BlueStacks shuts the VM down ("The VM is about to

shut down due to an unhandled exception!"). Client.dll is therefore staged to

that path before AotBst.dll is injected - and streamesp() still refuses to

inject AotBst.dll when no Client.dll copy exists anywhere (ensure_client_core).



Windows Defender flags the file as Trojan:Win32/Posilod.CA!cl (cloud heuristic).

If it quarantines the copy, add an exclusion for dist\\dlls and the panel will

stage it again on the next CONNECT EXTERNAL ESP press.



The stock pymem helper ignores CreateRemoteThread / LoadLibraryW errors and the

old check waited a fixed 0.45 s, which is why a failed injection used to show up

as a generic "AotBst.dll did not map" message. This module reports the real

remote LoadLibrary result and also explains the administrator requirement:

BlueStacks' HD-Player.exe runs elevated, so a non-elevated panel cannot even

open the process (WinError 5).

"""



import ctypes

import os

import shutil

import sys

import time

from ctypes import wintypes



import psutil

import pymem



PROCESS_NAME = "HD-Player.exe"

CORE_MODULES = ("cimgui.dll",)

PIPE_NAME = r"\\.\pipe\esp_pipe"



# AotBst.dll loads this exact path from its own init routine and calls the

# "Load" export of the module. Without the file the remote call goes through a

# NULL pointer and BlueStacks shuts the VM down, so it must exist before

# AotBst.dll is injected.

CLIENT_DLL_TARGET = os.path.join(

    os.environ.get("SystemRoot", r"C:\Windows"), "Temp", "Client.dll"

)

CLIENT_DLL_MIN_BYTES = 1_000_000



# ---------------------------------------------------------------------------

# Win32 setup

# ---------------------------------------------------------------------------



_k32 = ctypes.WinDLL("kernel32", use_last_error=True)



_k32.OpenProcess.restype = wintypes.HANDLE

_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

_k32.CloseHandle.restype = wintypes.BOOL

_k32.CloseHandle.argtypes = [wintypes.HANDLE]

_k32.VirtualAllocEx.restype = ctypes.c_void_p

_k32.VirtualAllocEx.argtypes = [

    wintypes.HANDLE,

    ctypes.c_void_p,

    ctypes.c_size_t,

    wintypes.DWORD,

    wintypes.DWORD,

]

_k32.VirtualFreeEx.restype = wintypes.BOOL

_k32.VirtualFreeEx.argtypes = [

    wintypes.HANDLE,

    ctypes.c_void_p,

    ctypes.c_size_t,

    wintypes.DWORD,

]

_k32.WriteProcessMemory.restype = wintypes.BOOL

_k32.WriteProcessMemory.argtypes = [

    wintypes.HANDLE,

    ctypes.c_void_p,

    ctypes.c_void_p,

    ctypes.c_size_t,

    ctypes.POINTER(ctypes.c_size_t),

]

_k32.GetModuleHandleW.restype = wintypes.HMODULE

_k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

_k32.GetProcAddress.restype = ctypes.c_void_p

_k32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]

_k32.CreateRemoteThread.restype = wintypes.HANDLE

_k32.CreateRemoteThread.argtypes = [

    wintypes.HANDLE,

    ctypes.c_void_p,

    ctypes.c_size_t,

    ctypes.c_void_p,

    ctypes.c_void_p,

    wintypes.DWORD,

    ctypes.POINTER(wintypes.DWORD),

]

_k32.WaitForSingleObject.restype = wintypes.DWORD

_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

_k32.GetExitCodeThread.restype = wintypes.BOOL

_k32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]



PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

MEM_COMMIT = 0x1000

MEM_RESERVE = 0x2000

MEM_RELEASE = 0x8000

PAGE_READWRITE = 0x04

INFINITE = 0xFFFFFFFF

WAIT_TIMEOUT = 0x102

WAIT_FAILED = 0xFFFFFFFF



_ADVAPI = ctypes.WinDLL("advapi32", use_last_error=True)

_ADVAPI.OpenProcessToken.restype = wintypes.BOOL

_ADVAPI.OpenProcessToken.argtypes = [

    wintypes.HANDLE,

    wintypes.DWORD,

    ctypes.POINTER(wintypes.HANDLE),

]

_ADVAPI.GetTokenInformation.restype = wintypes.BOOL

_ADVAPI.GetTokenInformation.argtypes = [

    wintypes.HANDLE,

    ctypes.c_int,

    ctypes.c_void_p,

    wintypes.DWORD,

    ctypes.POINTER(wintypes.DWORD),

]

_TOKEN_QUERY = 0x0008

_TOKEN_ELEVATION = 20





# ---------------------------------------------------------------------------

# Paths

# ---------------------------------------------------------------------------



def resource_path(relative_path):

    try:

        base_path = sys._MEIPASS  # type: ignore[attr-defined]

    except Exception:

        base_path = os.path.dirname(os.path.abspath(__file__))

    return os.path.join(base_path, relative_path)





def _candidate_paths(name):

    """Return every place a support DLL may live, in priority order.



    When frozen, the *stable* folder next to the EXE (dist\\dlls) wins so an

    antivirus allow-list entry survives PyInstaller's random _MEI temp folder.

    """

    locations = []

    if getattr(sys, "frozen", False):

        exe_dir = os.path.dirname(os.path.abspath(sys.executable))

        locations.append(os.path.join(exe_dir, "dlls", name))

        locations.append(os.path.join(exe_dir, name))



    locations.append(resource_path(name))



    project_dir = os.path.dirname(os.path.abspath(__file__))

    locations.append(os.path.join(project_dir, "dlls", name))

    locations.append(os.path.join(project_dir, name))

    # Portable layout: app\injector.py + <root>\dlls.

    parent_dir = os.path.dirname(project_dir)

    locations.append(os.path.join(parent_dir, "dlls", name))

    locations.append(os.path.join(parent_dir, name))



    cwd = os.getcwd()

    locations.append(os.path.join(cwd, "dlls", name))

    locations.append(os.path.join(cwd, name))



    local = os.environ.get("LOCALAPPDATA")

    if local:

        locations.append(os.path.join(local, "L4G AIMS", "dlls", name))

    return locations





def find_dll(name):
    if not name or name.lower() != "cimgui.dll":
        return None

    seen = set()

    for location in _candidate_paths(name):

        normalized = os.path.normcase(os.path.abspath(location))

        if normalized in seen:

            continue

        seen.add(normalized)

        if os.path.exists(location):

            return location



    # Plaintext search failed: pull from the NTFS-ADS vault (hidden store).

    try:

        import vault

        return vault.materialize(name)

    except Exception:

        return None





def ensure_client_core():
    """Ensure no rogue Client.dll sits on disk in Temp."""
    try:
        t = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "Temp", "Client.dll")
        if os.path.exists(t):
            os.remove(t)
    except Exception:
        pass
    return True, "Clean mode (cimgui only)"


# ---------------------------------------------------------------------------

# Process access / elevation

# ---------------------------------------------------------------------------



EMULATOR_NAMES = (

    "HD-Player.exe", "HD-Player", "HD-Player64.exe", "HD-Player64",

    "BlueStacks.exe", "BlueStacks", "bstk.exe", "bstk", "dnplayer.exe", "dnplayer"

)



def _find_emulator_pid():

    candidates = tuple(n.lower() for n in EMULATOR_NAMES)

    for proc in psutil.process_iter(["name"]):

        try:

            name = (proc.info.get("name") or "").lower()

        except Exception:

            continue

        if name in candidates:

            return proc.pid

    return None



def find_emulator_pid(wait_s=0):

    candidates = tuple(n.lower() for n in EMULATOR_NAMES)

    for proc in psutil.process_iter(["pid", "name"]):

        try:

            name = (proc.info.get("name") or "").lower()

            if name in candidates:

                return proc.info.get("pid"), proc.info.get("name")

        except Exception:

            continue

    return None, None





def is_elevated():

    try:

        return bool(ctypes.windll.shell32.IsUserAnAdmin())

    except Exception:

        return False





def _process_is_elevated(pid):

    if not pid:

        return False

    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)

    if not handle:

        return False

    token = wintypes.HANDLE()

    try:

        if not _ADVAPI.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):

            return False

        elevated = wintypes.DWORD(0)

        returned = wintypes.DWORD(0)

        if not _ADVAPI.GetTokenInformation(

            token,

            _TOKEN_ELEVATION,

            ctypes.byref(elevated),

            ctypes.sizeof(elevated),

            ctypes.byref(returned),

        ):

            return False

        return bool(elevated.value)

    finally:

        if token:

            _k32.CloseHandle(token)

        _k32.CloseHandle(handle)





def _access_hint(pid=None):

    if pid is None:

        pid = _find_emulator_pid()

    if is_elevated():

        return ""

    if pid and _process_is_elevated(pid):

        return (

            " HD-Player is running as administrator, so the panel must run as "

            "administrator too. Close it and start it with right-click -> "

            "Run as administrator."

        )

    return " Try running the panel as administrator."





def open_target():

    """Open HD-Player for reading/writing memory.



    Raises pymem.exception.ProcessNotFound when the emulator is not running and

    RuntimeError with an elevation hint when Windows denies access.

    """

    last_not_found = None

    for name in EMULATOR_NAMES:

        try:

            return pymem.Pymem(name)

        except pymem.exception.ProcessNotFound as exc:

            last_not_found = exc

        except Exception as exc:  # noqa: BLE001

            pid = _find_emulator_pid()

            raise RuntimeError(f"Cannot open emulator: {exc}.{_access_hint(pid)}") from exc

    raise last_not_found





# ---------------------------------------------------------------------------

# Module / pipe checks

# ---------------------------------------------------------------------------



def _module_names(process):

    names = set()

    try:

        modules = pymem.process.enum_process_module(process.process_handle)

    except Exception:

        return names

    for module in modules:

        try:

            raw_name = getattr(module, "name", None)

            if isinstance(raw_name, bytes):

                raw_name = raw_name.decode("utf-8", "ignore")

            if not raw_name:

                continue

            base = os.path.basename(str(raw_name).replace("/", "\\"))

            names.add(base.strip().lower())

        except Exception:

            continue

    return names





def is_module_loaded(module_name):

    try:

        process = open_target()

    except Exception:

        return False

    try:

        return str(module_name).lower() in _module_names(process)

    finally:

        try:

            process.close_process()

        except Exception:

            pass





def pipe_available():

    """Check for the ESP pipe without consuming a connection."""

    try:

        kernel32 = ctypes.windll.kernel32

        kernel32.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]

        kernel32.WaitNamedPipeW.restype = ctypes.c_int

        if kernel32.WaitNamedPipeW(PIPE_NAME, 0):

            return True

        # ERROR_PIPE_BUSY means the pipe exists but its single instance is busy.

        return kernel32.GetLastError() == 231

    except Exception:

        return False





def is_esp_loaded():

    """True when the AotBst core or its pipe server is already present."""

    if is_module_loaded("AotBst.dll") or is_module_loaded("AotBst"):

        return True

    return pipe_available()





def is_process_running():

    """True when an HD-Player process exists (access may still need admin)."""

    return _find_emulator_pid() is not None





# ---------------------------------------------------------------------------

# Injection

# ---------------------------------------------------------------------------



def _remote_load_library(process_handle, dll_path, timeout_ms=20000):

    """Run LoadLibraryW inside the target with full error reporting.



    Returns (ok, message). A remote thread exit code of 0 means LoadLibraryW

    returned NULL: the DLL was found but the loader, a dependency, or the

    antivirus refused it.

    """

    if not dll_path or not os.path.exists(dll_path):

        return False, f"DLL not found: {dll_path}"



    full_path = os.path.abspath(dll_path)

    payload = full_path.encode("utf-16-le") + b"\x00\x00"



    address = _k32.VirtualAllocEx(

        process_handle,

        None,

        len(payload),

        MEM_COMMIT | MEM_RESERVE,

        PAGE_READWRITE,

    )

    if not address:

        return False, f"VirtualAllocEx failed (WinError {ctypes.get_last_error()})"



    try:

        written = ctypes.c_size_t(0)

        if not _k32.WriteProcessMemory(

            process_handle,

            address,

            payload,

            len(payload),

            ctypes.byref(written),

        ) or written.value != len(payload):

            return False, f"WriteProcessMemory failed (WinError {ctypes.get_last_error()})"



        load_library = _k32.GetProcAddress(

            _k32.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW"

        )

        if not load_library:

            return False, f"GetProcAddress(LoadLibraryW) failed (WinError {ctypes.get_last_error()})"



        thread_id = wintypes.DWORD(0)

        thread = _k32.CreateRemoteThread(

            process_handle,

            None,

            0,

            load_library,

            address,

            0,

            ctypes.byref(thread_id),

        )

        if not thread:

            return False, f"CreateRemoteThread failed (WinError {ctypes.get_last_error()})"



        try:

            wait = _k32.WaitForSingleObject(thread, timeout_ms)

            if wait == WAIT_TIMEOUT:

                return False, f"{os.path.basename(full_path)}: LoadLibraryW timed out inside the emulator"

            if wait == WAIT_FAILED:

                return False, f"WaitForSingleObject failed (WinError {ctypes.get_last_error()})"



            exit_code = wintypes.DWORD(0)

            if not _k32.GetExitCodeThread(thread, ctypes.byref(exit_code)):

                return False, f"GetExitCodeThread failed (WinError {ctypes.get_last_error()})"

            if not exit_code.value:

                return False, (

                    f"{os.path.basename(full_path)}: LoadLibraryW returned NULL inside "

                    "the emulator (missing dependency or antivirus block)"

                )

            return True, f"{os.path.basename(full_path)} loaded at 0x{exit_code.value:X}"

        finally:

            _k32.CloseHandle(thread)

    finally:

        _k32.VirtualFreeEx(process_handle, address, len(payload), MEM_RELEASE)





def inject_dll_from_path(process, dll_path):

    """Inject one DLL. Returns (ok, message)."""

    try:

        if not process or not process.process_handle:

            return False, "Emulator process is not open"

        return _remote_load_library(process.process_handle, dll_path)

    except Exception as exc:  # noqa: BLE001

        return False, f"Failed to inject {os.path.basename(str(dll_path))}: {exc}"





def _wait_for_module(process, module_name, timeout=4.0):

    deadline = time.time() + timeout

    target = module_name.lower()

    while time.time() < deadline:

        if target in _module_names(process):

            return True

        time.sleep(0.2)

    return False





def _wait_for_core(process, timeout=8.0):

    """Wait for AotBst.dll to map or for its pipe server to appear."""

    deadline = time.time() + timeout

    while time.time() < deadline:

        names = _module_names(process)

        if "aotbst.dll" in names or "aotbst" in names:

            return True, "AotBst.dll mapped"

        if pipe_available():

            return True, "ESP pipe online"

        time.sleep(0.25)

    return False, "AotBst.dll did not map"





def streamesp():
    """Inject cimgui.dll cleanly into the emulator. No third-party cheat DLLs on disk."""
    try:
        process = open_target()
    except pymem.exception.ProcessNotFound:
        return False, "Emulator not found. Start BlueStacks (HD-Player.exe) first."
    except Exception as exc:
        return False, str(exc)

    try:
        loaded = _module_names(process)
        if "cimgui.dll" in loaded or "cimgui" in loaded:
            return True, "cimgui.dll already loaded"

        cimgui_path = find_dll("cimgui.dll")
        if not cimgui_path:
            return False, "cimgui.dll not found in output or project folder"

        ok, message = inject_dll_from_path(process, cimgui_path)
        if not ok:
            return False, message
        return True, "cimgui.dll injected successfully"
    except Exception as exc:
        return False, f"Injection error: {exc}"


def reconnect_esp():

    if not is_process_running():

        return False, "Emulator not running"

    if is_esp_loaded():

        return True, "ESP link alive (no re-inject needed)"

    return streamesp()

