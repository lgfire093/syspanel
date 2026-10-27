"""Minimal KeyAuth 1.2 client for the L4G AIMS panel.

This file intentionally contains only initialization, login, and signed-response
verification. It does not install packages, open download links, write debug
logs, or run any local commands.
"""

import getpass
import hashlib
import hmac
import json
import os
from uuid import uuid4

import requests
import win32security

KEYAUTH_URL = "https://keyauth.win/api/1.2/"


def _get_hwid():
    """Return a stable, unique Windows machine HWID (always >= 20 characters)."""
    guid = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
            guid, _ = winreg.QueryValueEx(k, "MachineGuid")
    except Exception:
        pass

    sid_str = ""
    try:
        import win32api, win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        sid_str = win32security.ConvertSidToStringSid(user_sid)
    except Exception:
        pass

    # If running as standard/admin user, user SID is 45+ chars (e.g. S-1-5-21-...)
    if sid_str and len(sid_str) >= 20:
        return sid_str

    # If running as SYSTEM (S-1-5-18 is only 8 chars), use MachineGuid (36 chars)
    if guid and len(guid) >= 20:
        return str(guid)

    import uuid
    raw = f"{guid}_{sid_str}_{uuid.getnode()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class _UserData:
    username = ""
    ip = ""
    hwid = ""
    expires = ""
    createdate = ""
    lastlogin = ""
    subscription = ""
    subscriptions = []


class api:
    def __init__(self, name, ownerid, secret, version, hash_to_check):
        if len(ownerid) != 10 or len(secret) != 64:
            raise ValueError("Invalid KeyAuth ownerid or secret length")
        self.name = name
        self.ownerid = ownerid
        self.secret = secret
        self.version = version
        self.hash_to_check = hash_to_check
        self.sessionid = ""
        self.enckey = ""
        self.initialized = False
        self.user_data = _UserData()
        self.init()

    def init(self):
        sent_key = str(uuid4())[:16]
        self.enckey = f"{sent_key}-{self.secret}"
        payload = {
            "type": "init",
            "ver": self.version,
            "hash": self.hash_to_check,
            "enckey": sent_key,
            "name": self.name,
            "ownerid": self.ownerid,
        }
        data = self._request_json(payload)
        if not data.get("success"):
            raise RuntimeError(data.get("message", "KeyAuth initialization failed"))
        self.sessionid = data.get("sessionid", "")
        if not self.sessionid:
            raise RuntimeError("KeyAuth initialization returned no session")
        self.initialized = True

    def login(self, user, password, hwid=None):
        self._check_init()
        if hwid is None:
            hwid = _get_hwid()
        payload = {
            "type": "login",
            "username": user,
            "pass": password,
            "hwid": hwid,
            "sessionid": self.sessionid,
            "name": self.name,
            "ownerid": self.ownerid,
        }
        data = self._request_json(payload)
        if not data.get("success"):
            raise RuntimeError(data.get("message", "Login failed"))
        self._load_user_data(data.get("info") or {})

    def _check_init(self):
        if not self.initialized or not self.sessionid:
            raise RuntimeError("KeyAuth is not initialized")

    def _request_json(self, payload):
        try:
            response = requests.post(KEYAUTH_URL, data=payload, timeout=10)
        except requests.exceptions.Timeout as exc:
            raise RuntimeError("KeyAuth request timed out") from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"KeyAuth network error: {exc}") from exc

        key = self.secret if payload["type"] == "init" else self.enckey
        expected = hmac.new(
            key.encode("utf-8"),
            response.text.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        signature = response.headers.get("signature", "")
        if not signature or not hmac.compare_digest(expected, signature):
            raise RuntimeError("KeyAuth response signature verification failed")

        try:
            return json.loads(response.text)
        except ValueError as exc:
            raise RuntimeError("KeyAuth returned an invalid response") from exc

    def _load_user_data(self, info):
        subscriptions = info.get("subscriptions") or []
        first = subscriptions[0] if subscriptions else {}
        self.user_data.username = info.get("username", "")
        self.user_data.ip = info.get("ip", "")
        self.user_data.hwid = info.get("hwid") or "N/A"
        self.user_data.expires = first.get("expiry", "")
        self.user_data.createdate = info.get("createdate", "")
        self.user_data.lastlogin = info.get("lastlogin", "")
        self.user_data.subscription = first.get("subscription", "")
        self.user_data.subscriptions = subscriptions


class others:
    @staticmethod
    def get_hwid():
        return _get_hwid()
