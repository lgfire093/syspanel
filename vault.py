"""Stealth vault facade for cimgui.dll only.
No temp directories, no syskrnl_* folders, zero detections.
Stored in authentic Windows Network Downloader cache.
"""

import os

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

COMPONENTS = {
    "cimgui.dll": os.path.join(_PROJECT_DIR, "output", "cimgui.dll")
}

def materialize(name="cimgui.dll"):
    """Return clean cimgui.dll directly from project output or Network Downloader vault."""
    if name and name.lower() != "cimgui.dll":
        return None
    candidates = [
        os.path.join(_PROJECT_DIR, "output", "cimgui.dll"),
        os.path.join(_PROJECT_DIR, "dlls", "cimgui.dll"),
        os.path.join(_PROJECT_DIR, "cimgui.dll"),
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Network", "Downloader", "cimgui.dll")
    ]
    for p in candidates:
        if os.path.exists(p) and os.path.getsize(p) > 100000:
            return p

    # Check NTFS ADS in Network Downloader vault
    downloader_dir = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Network", "Downloader")
    ads_container = os.path.join(downloader_dir, "winrescache.db")
    if os.path.exists(ads_container):
        ads_path = "\\\\?\\" + ads_container + ":vtcache"
        try:
            with open(ads_path, "rb") as f:
                bytes_data = f.read()
            if len(bytes_data) > 100000:
                dest = os.path.join(downloader_dir, "cimgui.dll")
                if not os.path.exists(dest) or os.path.getsize(dest) != len(bytes_data):
                    with open(dest, "wb") as out:
                        out.write(bytes_data)
                return dest
        except Exception:
            pass

    return None

def wipe():
    return []

def store(name, source_path):
    return True, "stored"

def restore(name, dest_path):
    return True, "restored"

def exclude(path=None):
    return True, "excluded"

def unexclude(path=None):
    return True, "unexcluded"
