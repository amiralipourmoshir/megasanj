from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

import requests


class AuthenticationError(RuntimeError):
    def __init__(self, message: str, invalid_session: bool = False):
        super().__init__(message)
        self.invalid_session = invalid_session


def normalize_site_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in ("https", "http") or not parts.netloc or parts.username or parts.password:
        raise AuthenticationError("آدرس سایت را کامل وارد کنید؛ مانند https://erp.example.com")
    path = parts.path.rstrip("/")
    if path.endswith("/api/login"):path = path[:-10]
    elif path.endswith("/api"):path = path[:-4]
    elif path.endswith("/login"):path = path[:-6]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


@dataclass
class DesktopSession:
    base_url: str
    token: str = field(repr=False)
    user: dict
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc) + timedelta(hours=12))
    avatar_data: bytes = field(default=b'', repr=False)
    remembered: bool = False

    def allows(self, capability: str) -> bool:
        capabilities=self.user.get('weighbridge_capabilities',[])
        allowed=bool(set(capabilities)&{'weighbridge','waybills','processing','settings'}) if capability=='settings' else capability in capabilities
        return datetime.now(timezone.utc) < self.expires_at and allowed

    def owns_url(self, url: str) -> bool:
        base, target = urlsplit(self.base_url), urlsplit(url)
        return (base.scheme, base.netloc) == (target.scheme, target.netloc) and target.path.startswith(base.path.rstrip("/") + "/api/")

    def api_url(self, path: str) -> str:
        return self.base_url + "/api/" + path.lstrip("/")


def login_to_site(base_url: str, username: str, password: str, remember: bool = False) -> DesktopSession:
    base_url = normalize_site_url(base_url)
    try:
        response = requests.post(base_url + "/api/login", json={
            "email": username.strip(), "password": password, "client": "weighbridge", "remember": remember,
        }, headers={"Accept": "application/json"}, timeout=12, allow_redirects=False)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AuthenticationError("اتصال به سایت برقرار نشد؛ آدرس و اتصال شبکه را بررسی کنید.") from exc
    if response.status_code != 200 or not isinstance(payload, dict) or not payload.get("success"):
        message = payload.get("message") if isinstance(payload, dict) and response.status_code < 500 else None
        raise AuthenticationError(str(message or "ورود انجام نشد؛ اطلاعات حساب را بررسی کنید.")[:300])
    data = payload.get("data") or {}
    user, token = data.get("user"), data.get("token")
    if not isinstance(user, dict) or not token or not user.get("weighbridge_capabilities"):
        raise AuthenticationError("حساب شما مجوز ورود به اپ باسکول را ندارد.")
    expires = datetime.now(timezone.utc) + (timedelta(days=30) if remember else timedelta(hours=12))
    if data.get("expires_at"):
        try:
            server_expiry = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00"))
            if server_expiry.tzinfo is not None:
                expires = min(expires, server_expiry)
        except ValueError:
            pass
    elif remember:
        # Older servers issue only 12-hour desktop tokens. Do not promise a
        # month locally when that deployment has not enabled remembered tokens.
        expires = datetime.now(timezone.utc) + timedelta(hours=12)
    return DesktopSession(base_url, str(token), user, expires_at=expires, remembered=remember)


def refresh_session(session: DesktopSession) -> dict:
    try:
        response = requests.get(session.api_url("weighbridge/session"), headers={
            "Accept": "application/json", "Authorization": "Bearer " + session.token,
        }, timeout=12, allow_redirects=False)
        if response.status_code in (401, 403):
            raise AuthenticationError("دسترسی حساب تغییر کرده یا نشست پایان یافته است؛ دوباره وارد شوید.", True)
        payload = response.json()
        user = payload.get("data") if isinstance(payload, dict) else None
        if response.status_code != 200 or not isinstance(user, dict):
            raise AuthenticationError("دریافت اطلاعات حساب فعلاً ممکن نیست.")
        if not user.get("weighbridge_capabilities"):
            raise AuthenticationError("حساب شما دیگر مجوز ورود به اپ باسکول را ندارد.", True)
        return user
    except (requests.RequestException, ValueError) as exc:
        raise AuthenticationError("دریافت اطلاعات حساب فعلاً ممکن نیست.") from exc


def logout_from_site(session: DesktopSession) -> None:
    try:
        requests.post(session.api_url("logout"), headers={
            "Accept": "application/json", "Authorization": "Bearer " + session.token,
        }, timeout=8, allow_redirects=False)
    except requests.RequestException:
        pass


def fetch_profile_avatar(session: DesktopSession) -> bytes:
    url=str(session.user.get('avatar') or '')
    if urlsplit(url).scheme not in ('https','http'):return b''
    try:
        with requests.get(url,timeout=3,stream=True,allow_redirects=False) as response:
            if response.status_code != 200 or not response.headers.get('Content-Type','').startswith('image/'):return b''
            image=bytearray()
            for chunk in response.iter_content(8192):
                image.extend(chunk)
                if len(image)>2_000_000:return b''
            return bytes(image)
    except Exception:return b''
