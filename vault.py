"""Stealth vault facade for cimgui.dll only.
No temp directories, no syskrnl_* folders, zero detections.
"""

import os

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

COMPONENTS = {
    "cimgui.dll": os.path.join(_PROJECT_DIR, "output", "cimgui.dll")
}

def materialize(name="cimgui.dll"):
    """Return clean cimgui.dll directly from project output or dlls folder.
    Leaves 0 files in Temp, AppData, or LocalAppData.
    """
    if name and name.lower() != "cimgui.dll":
        return None
    candidates = [
        os.path.join(_PROJECT_DIR, "output", "cimgui.dll"),
        os.path.join(_PROJECT_DIR, "dlls", "cimgui.dll"),
        os.path.join(_PROJECT_DIR, "cimgui.dll"),
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Network", "Downloader", "cimgui.dll")
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
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
