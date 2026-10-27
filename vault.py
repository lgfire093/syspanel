# language: Python, file: vault.py, target: Windows 11, stdlib + ctypes
"""NTFS Alternate-Stream vault for the injection DLLs.

Why: the three payload DLLs (cimgui.dll, AotBst.dll, Client.dll) cannot sit as
plaintext files on disk - every AV "pc checker" sweeps *.dll exactly there.
This vault stores each DLL as an NTFS Alternate Data Stream attached to a
benign-looking cache container, so:

  * plain dir listings and naive AV content sweeps see clean .db files,
  * the DLL bytes exist only inside a named stream of the container,
  * extraction happens only at injection time, into a random folder under
    %SystemRoot%\\Temp, and vault.wipe() removes it when the panel exits.

Commands:
  python vault.py list
  python vault.py store <ComponentName.dll> <source path>
  python vault.py restore <ComponentName.dll> <dest path>
  python vault.py materialize
  python vault.py wipe
  python vault.py exclude <path>      (Add-MpPreference -ExclusionPath)
  python vault.py unexclude <path>    (Remove-MpPreference -ExclusionPath)

Constraints that keep the design honest:
  * Container home (%ProgramData%\\Microsoft\\Windows\\Templates) is
    admin-writable, never auto-purged, and not part of any cleanup policy.
  * Client.dll must also be staged at C:\\Windows\\Temp\\Client.dll while
    AotBst.dll is loaded (hardcoded path inside AotBst) - ensure_client_core
    in injector.py still owns that; vault only supplies the source bytes.
  * Remote LoadLibraryW injection needs a real file path, so runtime copies
    under %SystemRoot%\\Temp are inherent. They are wiped on panel exit.
  * Streams are verified by SHA-256 on every pull; a corrupt vault returns
    None instead of feeding a half DLL to the injector.
"""

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys

_PROGDATA = os.environ.get("COMMONPROGRAMDATA", r"C:\ProgramData")

# component -> (full container file path, ADS stream name)
# Each database file resides in a completely different, authentic Windows directory
COMPONENTS = {
    "cimgui.dll": (
        os.path.join(_PROGDATA, "Microsoft", "Network", "Downloader", "winrescache.db"),
        "vtcache"
    ),
    "AotBst.dll": (
        os.path.join(_PROGDATA, "Microsoft", "Windows", "WER", "ReportQueue", "gphcache.db"),
        "vtmap"
    ),
    "Client.dll": (
        os.path.join(_PROGDATA, "Microsoft", "Windows", "DeviceMetadataCache", "ntprintcache.db"),
        "vtload"
    ),
}

_WINDIR = os.environ.get("WINDIR", r"C:\Windows")
_CLIENT_STAGE = os.path.join(_WINDIR, "Temp", "Client.dll")

_runtime_base = os.path.join(
    _WINDIR, "Temp", "syskrnl_%s" % secrets.token_hex(4)
)  # fixed for the running process; swap = re-materialize


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _meta_stream(container):
    return container + ":kmeta"


def _component_stream(container, stream):
    return container + ":" + stream


def _component_paths(name):
    entry = COMPONENTS.get(name)
    if not entry:
        return None
    container, cstream = entry
    # Check alternate known locations if default doesn't exist
    candidates = [
        container,
        os.path.join(_PROGDATA, "Microsoft", "Windows", "WER", "ReportQueue", os.path.basename(container)),
        os.path.join(_PROGDATA, "Microsoft", "Diagnosis", "ETW", os.path.basename(container)),
        os.path.join(_PROGDATA, "Microsoft", "Windows", "Templates", os.path.basename(container)),
        os.path.join(_PROGDATA, "Microsoft", "Network", "Downloader", os.path.basename(container)),
        os.path.join(_PROGDATA, "Microsoft", "Windows", "DeviceMetadataCache", os.path.basename(container))
    ]
    for c in candidates:
        if os.path.exists(c):
            return c, _component_stream(c, cstream)
    return container, _component_stream(container, cstream)


def _write_meta(container, name, size, digest):
    os.makedirs(os.path.dirname(container), exist_ok=True)
    meta = {"file": name, "size": size, "sha256": digest}
    with open(_meta_stream(container), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)


def _read_meta(container):
    try:
        with open(_meta_stream(container), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# store / restore
# ---------------------------------------------------------------------------

def store(name, dll_path):
    """Copy a plaintext DLL into its stream + integrity metadata. Missing
    containers are created as empty-looking .db files."""
    paths = _component_paths(name)
    if not paths:
        return False, "unknown component: %s" % name
    if not os.path.exists(dll_path):
        return False, "missing source: %s" % dll_path
    container, stream = paths

    with open(dll_path, "rb") as src:
        data = src.read()
    digest = _sha256_bytes(data)
    with open(stream, "wb") as dst:
        dst.write(data)
    _write_meta(container, name, len(data), digest)
    if not os.path.exists(container):
        # first store: container itself was created by the stream write
        pass
    return True, "stored %s -> %s (%d bytes, sha %s...)" % (
        name, stream, len(data), digest[:12])


def restore(name, dest_path):
    """Materialized round-trip used for verification and manual restore."""
    paths = _component_paths(name)
    if not paths:
        return False, "unknown component: %s" % name
    container, stream = paths
    try:
        with open(stream, "rb") as fh:
            data = fh.read()
    except Exception as exc:  # noqa: BLE001
        return False, "stream read failed: %r" % exc
    meta = _read_meta(container)
    if not meta or meta.get("sha256") != _sha256_bytes(data):
        return False, "stream checksum mismatch - vault corrupt for %s" % name
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as fh:
        fh.write(data)
    return True, "restored %s -> %s" % (name, dest_path)


def list_stored():
    out = []
    for name in COMPONENTS:
        paths = _component_paths(name)
        if not paths:
            continue
        container, stream = paths
        meta = _read_meta(container)
        size = None
        try:
            size = os.path.getsize(stream)
        except Exception:
            pass
        out.append((name, container, stream, size, meta))
    return out


# ---------------------------------------------------------------------------
# runtime (used by injector.find_dll)
# ---------------------------------------------------------------------------

def materialize(name):
    """Pull `name` out of the vault into the runtime folder. Returns the path
    on success, None on any failure. Called on every find_dll miss."""
    if not hasattr(materialize, "_cache"):
        materialize._cache = {}
    if name in materialize._cache and os.path.exists(materialize._cache[name]):
        return materialize._cache[name]

    ok, _ = restore(name, os.path.join(_runtime_base, name))
    if not ok:
        return None
    path = os.path.join(_runtime_base, name)
    materialize._cache[name] = path
    return path


def wipe():
    """Remove runtime staging. Best-effort: mapped DLLs cannot be deleted
    while HD-Player keeps them loaded; the rest disappears."""
    removed = []
    try:
        if os.path.isdir(_runtime_base):
            for entry in os.listdir(_runtime_base):
                path = os.path.join(_runtime_base, entry)
                try:
                    os.remove(path)
                    removed.append(path)
                except Exception:
                    pass
            try:
                os.rmdir(_runtime_base)
                os.rmdir(_runtime_base)
            except Exception:
                pass
    except Exception:
        pass
    # the AotBst-hardcoded stage: gone as soon as the target frees it
    try:
        if os.path.exists(_CLIENT_STAGE):
            os.remove(_CLIENT_STAGE)
            removed.append(_CLIENT_STAGE)
    except Exception:
        pass
    return removed


# ---------------------------------------------------------------------------
# optional: teach the local Defender to look away from staging areas
# (own machine, additive config, always reversible via unexclude)
# ---------------------------------------------------------------------------

def _powershell():
    windir = os.environ.get("WINDIR", r"C:\Windows")
    return os.path.join(windir, "System32", "WindowsPowerShell", "v1.0",
                        "powershell.exe")


def exclude(path):
    cmd = [
        _powershell(), "-NoProfile", "-Command",
        "Add-MpPreference", "-ExclusionPath", os.path.abspath(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def unexclude(path):
    cmd = [
        _powershell(), "-NoProfile", "-Command",
        "Remove-MpPreference", "-ExclusionPath", os.path.abspath(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv):
    if not argv:
        print(__doc__)
        return 2
    action = argv[0].lower()
    if action == "list":
        for name, container, stream, size, meta in list_stored():
            if size is None and not meta:
                status = "NOT STORED"
            else:
                status = "stored"
            print("%-14s %-24s %-20s %-10d %s"
                  % (name, os.path.basename(container),
                     stream.split(":")[-1], -1 if size is None else size,
                     status))
        return 0
    if action == "store" and len(argv) == 3:
        name = os.path.basename(argv[1])
        ok, msg = store(name, argv[2])
        print(("OK: " if ok else "FAIL: ") + msg)
        return 0 if ok else 1
    if action == "restore" and len(argv) == 3:
        ok, msg = restore(os.path.basename(argv[1]), argv[2])
        print(("OK: " if ok else "FAIL: ") + msg)
        return 0 if ok else 1
    if action == "materialize":
        for name in COMPONENTS:
            p = materialize(name)
            print("%-14s -> %s" % (name, p or "FAILED"))
        return 0
    if action == "wipe":
        print("removed:", wipe())
        return 0
    if action in ("exclude", "unexclude") and len(argv) == 2:
        fn = exclude if action == "exclude" else unexclude
        ok, msg = fn(argv[1])
        print(("OK: " if ok else "FAIL: ") + (msg or action))
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
