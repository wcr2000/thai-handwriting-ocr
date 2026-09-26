"""ระบบล็อกอินแบบง่าย: บัญชีคงที่ 2 บัญชีจาก .env + cookie ที่เซ็นด้วย HMAC

ตั้งใจให้ไม่มี dependency เพิ่มและไม่มีตาราง user ในฐานข้อมูล เพราะงานนี้มีผู้ใช้ไม่กี่คน
แต่ยังต้องทนต่อการเดารหัสผ่าน:

* รหัสผ่านเก็บเป็น scrypt hash เท่านั้น (ไม่เคยเก็บ plaintext แม้ใน .env)
* scrypt ถูกตั้งค่าให้ช้าโดยตั้งใจ (~100ms/ครั้ง) การเดาแบบ brute force จึงแพงมาก
* ล็อกทั้งราย IP และรายชื่อผู้ใช้ เมื่อพลาดหลายครั้ง และหน่วงเวลาเพิ่มแบบทวีคูณ
* ผู้ใช้ที่ไม่มีอยู่จริงก็ยัง verify กับ hash หลอกเสมอ เพื่อไม่ให้เดาได้จากเวลาตอบกลับ
* เทียบค่าทุกอย่างด้วย compare_digest เพื่อไม่ให้รั่วผ่าน timing
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
SESSION_TTL = 12 * 3600  # 12 ชั่วโมง พอดีกับหนึ่งกะทำงาน
COOKIE_NAME = "ocrslip_session"

# ค่าเริ่มต้นของการล็อก: พลาดเกิน MAX_FAILS ครั้งใน WINDOW วินาที แล้วโดนล็อก
MAX_FAILS = 5
WINDOW = 15 * 60
BASE_LOCK = 30           # ล็อกครั้งแรก 30 วินาที แล้วเพิ่มเป็นเท่าตัว
MAX_LOCK_IP = 30 * 60    # เพดานของการล็อกราย IP
# เพดานของการล็อกรายชื่อผู้ใช้ตั้งไว้ต่ำกว่ามาก เพราะคนร้ายยิงชื่อ admin จาก IP ไหนก็ได้
# ถ้าล็อกยาวเท่ากัน เท่ากับเปิดช่องให้กันเจ้าหน้าที่ตัวจริงเข้าระบบตอนฉุกเฉิน
MAX_LOCK_USER = 3 * 60

# hash หลอกสำหรับ username ที่ไม่มีอยู่ — ให้เสียเวลาเท่ากับกรณีมีจริง
_DUMMY_HASH = None


# ---------- hashing ----------

def hash_password(password: str) -> str:
    """คืน string ที่เอาไปใส่ .env ได้เลย (ไม่มีรหัสผ่านจริงอยู่ข้างใน)"""
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


# ---------- บัญชีผู้ใช้ ----------

@dataclass(frozen=True)
class User:
    username: str
    role: str  # "admin" | "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def load_accounts() -> dict[str, tuple[str, str]]:
    """อ่านบัญชีจาก env -> {username: (password_hash, role)}"""
    accounts: dict[str, tuple[str, str]] = {}
    for prefix, role in (("ADMIN", "admin"), ("USER", "user")):
        name = os.getenv(f"{prefix}_USERNAME", "").strip()
        pw_hash = os.getenv(f"{prefix}_PASSWORD_HASH", "").strip()
        if name and pw_hash:
            accounts[name] = (pw_hash, role)
    return accounts


# ---------- cookie ที่เซ็นแล้ว ----------

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
    """คืน User ถ้าลายเซ็นถูกและยังไม่หมดอายุ — ไม่งั้นคืน None"""
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
    return User(username=data.get("u", ""), role=data.get("r", "user"))


# ---------- กันเดารหัสผ่าน ----------

@dataclass
class Throttle:
    """นับความพยายามที่ล้มเหลว แยกตาม key (ใช้ทั้ง IP และ username)

    เก็บในหน่วยความจำของ process เดียว — พอสำหรับงานนี้ที่รันอินสแตนซ์เดียว
    ถ้าขยายเป็นหลายอินสแตนซ์ต้องย้ายไป Redis หรือตารางใน Postgres
    """

    fails: dict[str, list[float]] = field(default_factory=dict)
    locked_until: dict[str, float] = field(default_factory=dict)

    def locked_for(self, key: str) -> int:
        """เหลือเวลาโดนล็อกกี่วินาที (0 = ไม่โดนล็อก)"""
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
        """ตัดรายการเก่าทิ้ง ไม่ให้ dict โตไม่จำกัดเมื่อโดนยิงนาน ๆ"""
        now = time.time()
        self.fails = {
            k: [t for t in v if now - t < WINDOW]
            for k, v in self.fails.items()
            if any(now - t < WINDOW for t in v)
        }
        self.locked_until = {k: t for k, t in self.locked_until.items() if t > now}


throttle = Throttle()


def authenticate(username: str, password: str, client_ip: str) -> tuple[User | None, str]:
    """คืน (User|None, ข้อความ error ภาษาไทย)"""
    username = (username or "").strip()
    ip_key, user_key = f"ip:{client_ip}", f"user:{username.lower()}"

    for key in (ip_key, user_key):
        if (wait := throttle.locked_for(key)) > 0:
            return None, f"พยายามเข้าสู่ระบบผิดหลายครั้งเกินไป กรุณารออีก {wait} วินาที"

    accounts = load_accounts()
    if not accounts:
        return None, "ยังไม่ได้ตั้งค่าบัญชีผู้ใช้ (ADMIN_USERNAME / ADMIN_PASSWORD_HASH ใน .env)"

    stored, role = accounts.get(username, (_dummy_hash(), "user"))
    ok = verify_password(password or "", stored) and username in accounts

    if not ok:
        lock = max(
            throttle.record_failure(ip_key, MAX_LOCK_IP),
            throttle.record_failure(user_key, MAX_LOCK_USER),
        )
        throttle.cleanup()
        time.sleep(0.25)  # หน่วงคงที่ กันการยิงรัว ๆ
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
        print("วิธีใช้: python -m ocrslip.auth hash '<รหัสผ่าน>'")
        print("แล้วเอาค่าที่ได้ไปใส่ ADMIN_PASSWORD_HASH หรือ USER_PASSWORD_HASH ใน .env")
