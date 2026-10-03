"""Minimal login: three fixed accounts (admin / staff / approver) from .env + an HMAC-signed cookie.

Deliberately adds no dependency and no user table, because this job has only a handful
of users — but it still has to withstand password guessing:

* Passwords are stored only as scrypt hashes (plaintext is never stored, not even in .env)
* scrypt is tuned to be deliberately slow (~100ms per attempt), making brute force expensive
* Lockout applies per IP and per username after repeated failures, with exponential backoff
* Non-existent users are still verified against a decoy hash, so response time reveals nothing
* Every comparison goes through compare_digest, so nothing leaks through timing
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
SESSION_TTL = 12 * 3600  # 12 hours, about one work shift
COOKIE_NAME = "ocrslip_session"

# Lockout settings: more than MAX_FAILS failures within WINDOW seconds triggers a lock
MAX_FAILS = 5
WINDOW = 15 * 60
BASE_LOCK = 30           # first lock lasts 30 seconds, then doubles
MAX_LOCK_IP = 30 * 60    # ceiling for the per-IP lock
# The per-username ceiling is set much lower, because an attacker can hammer the admin
# name from any IP. An equally long lock would hand them a way to keep real staff out
# of the system during an emergency.
MAX_LOCK_USER = 3 * 60

# Decoy hash for non-existent usernames — costs the same time as a real one
_DUMMY_HASH = None


# ---------- hashing ----------

def hash_password(password: str) -> str:
    """Return a string ready to paste into .env (it contains no actual password)"""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p),
        )
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(16))
    return _DUMMY_HASH


# ---------- user accounts ----------

@dataclass(frozen=True)
class User:
    username: str
    role: str  # "admin" | "user" | "approver"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_approver(self) -> bool:
        """Labelling volunteer: sees only upload and the review queue, never system-wide data"""
        return self.role == "approver"


def load_accounts() -> dict[str, tuple[str, str]]:
    """Read accounts from the environment -> {username: (password_hash, role)}"""
    accounts: dict[str, tuple[str, str]] = {}
    for prefix, role in (("ADMIN", "admin"), ("USER", "user"), ("APPROVE", "approver")):
        name = os.getenv(f"{prefix}_USERNAME", "").strip()
        pw_hash = os.getenv(f"{prefix}_PASSWORD_HASH", "").strip()
        if name and pw_hash:
            accounts[name] = (pw_hash, role)
    return accounts


# ---------- signed cookie ----------

def _sign(payload: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    enc = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return f"{enc(payload)}.{enc(sig)}"


def make_token(user: User, secret: str) -> str:
    payload = json.dumps(
        {"u": user.username, "r": user.role, "exp": int(time.time()) + SESSION_TTL},
        separators=(",", ":"),
    ).encode()
    return _sign(payload, secret)


def read_token(token: str, secret: str) -> User | None:
    """Return the User when the signature is valid and unexpired, otherwise None"""
    try:
        payload_b64, sig_b64 = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        payload = base64.urlsafe_b64decode(pad(payload_b64))
        sig = base64.urlsafe_b64decode(pad(sig_b64))
    except (ValueError, TypeError):
        return None

    expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    # A payload with no role is an old or forged token: fall back to the least privilege
    return User(username=data.get("u", ""), role=data.get("r", "approver"))


# ---------- password-guessing defence ----------

@dataclass
class Throttle:
    """Count failed attempts per key (keyed by both IP and username).

    Held in a single process's memory, which is enough for this job's single instance.
    Scaling to several instances would mean moving this to Redis or a Postgres table.
    """

    fails: dict[str, list[float]] = field(default_factory=dict)
    locked_until: dict[str, float] = field(default_factory=dict)

    def locked_for(self, key: str) -> int:
        """Seconds of lockout remaining (0 = not locked)"""
        return max(0, int(self.locked_until.get(key, 0) - time.time()))

    def record_failure(self, key: str, max_lock: int = MAX_LOCK_IP) -> int:
        now = time.time()
        recent = [t for t in self.fails.get(key, []) if now - t < WINDOW]
        recent.append(now)
        self.fails[key] = recent
        if len(recent) >= MAX_FAILS:
            over = len(recent) - MAX_FAILS
            lock = min(BASE_LOCK * (2**over), max_lock)
            self.locked_until[key] = now + lock
            return int(lock)
        return 0

    def reset(self, key: str) -> None:
        self.fails.pop(key, None)
        self.locked_until.pop(key, None)

    def cleanup(self) -> None:
        """Drop stale entries so the dicts cannot grow without bound under a long attack"""
        now = time.time()
        self.fails = {
            k: [t for t in v if now - t < WINDOW]
            for k, v in self.fails.items()
            if any(now - t < WINDOW for t in v)
        }
        self.locked_until = {k: t for k, t in self.locked_until.items() if t > now}


throttle = Throttle()


def authenticate(username: str, password: str, client_ip: str) -> tuple[User | None, str]:
    """Return (User|None, error message). The message is Thai: it is shown to staff."""
    username = (username or "").strip()
    ip_key, user_key = f"ip:{client_ip}", f"user:{username.lower()}"

    for key in (ip_key, user_key):
        if (wait := throttle.locked_for(key)) > 0:
            return None, f"พยายามเข้าสู่ระบบผิดหลายครั้งเกินไป กรุณารออีก {wait} วินาที"

    accounts = load_accounts()
    if not accounts:
        return None, "ยังไม่ได้ตั้งค่าบัญชีผู้ใช้ (ADMIN_USERNAME / ADMIN_PASSWORD_HASH ใน .env)"

    stored, role = accounts.get(username, (_dummy_hash(), "approver"))
    ok = verify_password(password or "", stored) and username in accounts

    if not ok:
        lock = max(
            throttle.record_failure(ip_key, MAX_LOCK_IP),
            throttle.record_failure(user_key, MAX_LOCK_USER),
        )
        throttle.cleanup()
        time.sleep(0.25)  # fixed delay, to blunt rapid-fire attempts
        if lock:
            return None, f"ผิดหลายครั้งเกินไป ระบบล็อกชั่วคราว {lock} วินาที"
        return None, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"

    throttle.reset(ip_key)
    throttle.reset(user_key)
    return User(username=username, role=role), ""


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "hash":
        print(hash_password(sys.argv[2]))
    else:
        print("usage: python -m ocrslip.auth hash '<password>'")
        print("then put the result in ADMIN_PASSWORD_HASH / USER_PASSWORD_HASH / APPROVE_PASSWORD_HASH in .env")
