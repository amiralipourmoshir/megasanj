"""Remember a revocable session using Windows user-bound DPAPI encryption."""
import ctypes
from ctypes import wintypes
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .auth import DesktopSession


def session_path():
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share")
    edition = 'WeighbridgeOdoo' if os.environ.get('MEGASANJ_EDITION') == 'odoo' else 'Weighbridge'
    return root / 'MegaFoolad' / edition / 'remembered-session.bin'


def _crypt(data, decrypt=False):
    if os.name != "nt":
        raise OSError("Remembered sessions require Windows credential protection")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel32.LocalFree(output.data)


def forget_session():
    session_path().unlink(missing_ok=True)


def save_session(session):
    if not session.remembered:
        forget_session()
        return
    payload = json.dumps({"version": 1, "base_url": session.base_url, "token": session.token,
                          "user": session.user, "expires_at": session.expires_at.isoformat()}, ensure_ascii=False).encode()
    encrypted = _crypt(payload)
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encrypted)
    temporary.replace(path)


def load_session():
    path = session_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(_crypt(path.read_bytes(), decrypt=True))
        expires = datetime.fromisoformat(payload["expires_at"])
        if payload.get("version") != 1 or expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            forget_session()
            return None
        if not payload.get("token") or not isinstance(payload.get("user"), dict):
            raise ValueError("Invalid remembered session")
        return DesktopSession(payload["base_url"], payload["token"], payload["user"], expires_at=expires, remembered=True)
    except (OSError, ValueError, KeyError, TypeError):
        forget_session()
        return None
