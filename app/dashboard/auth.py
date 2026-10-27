"""Dashboard authentication: owner only.

Two ways in, both ending in the same signed session cookie:
1. Password: PBKDF2-SHA256 hash in DASHBOARD_PASSWORD_HASH (stdlib, 600k iterations).
2. Telegram: the owner sends /dashboard to the bot and gets a link that is valid for
   10 minutes and works once. Opening it shows a button that POSTs the token, so link
   previews and prefetchers cannot use it up.

Sessions are itsdangerous-signed with DASHBOARD_SECRET_KEY and bound to a fingerprint of
the password hash: changing the password signs everyone out.

    python -m app.dashboard.auth hash-password
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets
import sys
import time
from collections import deque

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings

COOKIE = "pva_dash"
CSRF_COOKIE = "pva_csrf"
TG_LINK_TTL_S = 600
PBKDF2_ITERATIONS = 600_000


# ---------------------------------------------------------------- passwords ---

def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    # ':' separators and URL-safe base64: no '$', which docker compose would try to interpolate.
    return f"pbkdf2_sha256:{iterations}:{b64(salt)}:{b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, hash_b64 = stored.split(":")
        if algo != "pbkdf2_sha256":
            return False
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        salt, expected = base64.urlsafe_b64decode(pad(salt_b64)), base64.urlsafe_b64decode(pad(hash_b64))
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


# ----------------------------------------------------------------- sessions ---

def _secret() -> str:
    key = get_settings().dashboard_secret_key.get_secret_value()
    if len(key) < 16:
        raise RuntimeError("DASHBOARD_SECRET_KEY must be set (16+ characters)")
    return key


def _fingerprint() -> str:
    return hashlib.sha256(get_settings().dashboard_password_hash.get_secret_value().encode()).hexdigest()[:16]


def make_session(method: str) -> str:
    return URLSafeTimedSerializer(_secret(), salt="dash-session").dumps({"sub": "owner", "m": method, "fp": _fingerprint()})


def read_session(token: str | None) -> dict | None:
    if not token:
        return None
    max_age = get_settings().dashboard_session_hours * 3600
    try:
        data = URLSafeTimedSerializer(_secret(), salt="dash-session").loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if data.get("sub") != "owner" or data.get("fp") != _fingerprint():
        return None
    return data


def cookie_secure() -> bool:
    return get_settings().dashboard_public_url.startswith("https://")


# ---------------------------------------------------------- telegram links ---

class UsedTokens:
    """Single-use enforcement for login links (in memory; one dashboard process)."""

    def __init__(self):
        self._seen: dict[str, float] = {}

    def claim(self, nonce: str, now: float | None = None) -> bool:
        now = now or time.time()
        self._seen = {n: t for n, t in self._seen.items() if now - t < TG_LINK_TTL_S}
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True


used_tokens = UsedTokens()


def make_login_token(telegram_user_id: int) -> str:
    return URLSafeTimedSerializer(_secret(), salt="dash-tg-login").dumps({"uid": telegram_user_id, "n": secrets.token_urlsafe(12)})


def login_link(telegram_user_id: int) -> str:
    return f"{get_settings().dashboard_public_url}/login/telegram?token={make_login_token(telegram_user_id)}"


def check_login_token(token: str, *, consume: bool) -> bool:
    try:
        data = URLSafeTimedSerializer(_secret(), salt="dash-tg-login").loads(token, max_age=TG_LINK_TTL_S)
    except (BadSignature, SignatureExpired):
        return False
    owner = get_settings().owner_telegram_id
    if owner is None or data.get("uid") != owner:
        return False
    return used_tokens.claim(data["n"]) if consume else True


# ----------------------------------------------------- login rate limiting ---

class LoginThrottle:
    """5 failures per IP and 20 overall per 15 minutes."""

    WINDOW = 900

    def __init__(self, per_ip: int = 5, total: int = 20):
        self.per_ip, self.total = per_ip, total
        self._fails: deque[tuple[float, str]] = deque()

    def _prune(self, now: float) -> None:
        while self._fails and now - self._fails[0][0] > self.WINDOW:
            self._fails.popleft()

    def blocked(self, ip: str, now: float | None = None) -> bool:
        now = now or time.time()
        self._prune(now)
        return len(self._fails) >= self.total or sum(1 for _, i in self._fails if i == ip) >= self.per_ip

    def fail(self, ip: str, now: float | None = None) -> None:
        self._fails.append((now or time.time(), ip))


throttle = LoginThrottle()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "hash-password":
        print("usage: python -m app.dashboard.auth hash-password", file=sys.stderr)
        raise SystemExit(2)
    pw = getpass.getpass("Dashboard password: ")
    if len(pw) < 12:
        raise SystemExit("Use at least 12 characters.")
    if getpass.getpass("Repeat: ") != pw:
        raise SystemExit("Passwords do not match.")
    print("\nAdd this line to .env:\n")
    print(f"DASHBOARD_PASSWORD_HASH={hash_password(pw)}")


if __name__ == "__main__":
    main()
