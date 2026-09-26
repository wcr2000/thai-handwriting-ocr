"""ทดสอบระบบล็อกอิน — จุดที่พังแล้วเสียหายที่สุดของทั้งระบบ"""

import time

import pytest

from ocrslip import auth


def test_hash_never_contains_plaintext():
    h = auth.hash_password("sup3r-secret")
    assert "sup3r-secret" not in h
    assert h.startswith("scrypt$")


def test_verify_roundtrip():
    h = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("Correct horse", h)
    assert not auth.verify_password("", h)


def test_verify_rejects_garbage_hash():
    assert not auth.verify_password("x", "not-a-hash")
    assert not auth.verify_password("x", "md5$1$2$3$4$5")


def test_token_roundtrip():
    u = auth.User("admin", "admin")
    tok = auth.make_token(u, "secret-key")
    got = auth.read_token(tok, "secret-key")
    assert got == u and got.is_admin


@pytest.mark.parametrize("mutate", [
    lambda t: t[:-2] + "xx",          # แก้ลายเซ็น
    lambda t: "x" + t[1:],            # แก้ payload
    lambda t: t.replace(".", ""),     # รูปแบบผิด
    lambda t: "",                     # ว่าง
])
def test_tampered_token_is_rejected(mutate):
    tok = auth.make_token(auth.User("admin", "admin"), "secret-key")
    assert auth.read_token(mutate(tok), "secret-key") is None


def test_token_signed_with_other_key_is_rejected():
    tok = auth.make_token(auth.User("admin", "admin"), "key-a")
    assert auth.read_token(tok, "key-b") is None


def test_expired_token_is_rejected(monkeypatch):
    monkeypatch.setattr(auth, "SESSION_TTL", -1)
    tok = auth.make_token(auth.User("admin", "admin"), "k")
    assert auth.read_token(tok, "k") is None


def test_role_cannot_be_forged_by_editing_payload():
    """เปลี่ยน role ใน payload แล้วลายเซ็นต้องไม่ผ่าน"""
    import base64, json
    tok = auth.make_token(auth.User("staff", "user"), "k")
    payload_b64, sig = tok.split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    payload["r"] = "admin"
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    assert auth.read_token(f"{forged}.{sig}", "k") is None


def test_lockout_escalates_then_caps():
    t = auth.Throttle()
    locks = [t.record_failure("ip:test", auth.MAX_LOCK_IP) for _ in range(12)]
    assert locks[:4] == [0, 0, 0, 0]          # ยังไม่ล็อกใน 4 ครั้งแรก
    assert locks[4] == auth.BASE_LOCK          # ครั้งที่ 5 เริ่มล็อก
    assert locks[5] > locks[4]                 # เพิ่มเป็นเท่าตัว
    assert max(locks) == auth.MAX_LOCK_IP      # แต่ไม่เกินเพดาน


def test_username_lockout_is_capped_lower_than_ip():
    """กันไม่ให้คนร้ายยิงชื่อ admin เพื่อล็อกเจ้าหน้าที่ตัวจริงออกจากระบบนาน ๆ"""
    t = auth.Throttle()
    user_locks = [t.record_failure("user:admin", auth.MAX_LOCK_USER) for _ in range(12)]
    assert max(user_locks) == auth.MAX_LOCK_USER < auth.MAX_LOCK_IP


def test_successful_login_clears_counter():
    t = auth.Throttle()
    for _ in range(4):
        t.record_failure("ip:x")
    t.reset("ip:x")
    assert t.locked_for("ip:x") == 0
    assert t.record_failure("ip:x") == 0


def test_cleanup_bounds_memory():
    t = auth.Throttle()
    t.fails = {f"ip:{i}": [time.time() - auth.WINDOW - 1] for i in range(100)}
    t.cleanup()
    assert t.fails == {}


def test_unknown_user_and_wrong_password_give_same_message(monkeypatch):
    """ข้อความต้องเหมือนกัน ไม่งั้นเดาได้ว่าบัญชีไหนมีอยู่จริง"""
    h = auth.hash_password("pw")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", h)
    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()
    _, msg_wrong_pw = auth.authenticate("admin", "nope", "10.0.0.1")
    _, msg_no_user = auth.authenticate("ghost", "nope", "10.0.0.2")
    assert msg_wrong_pw == msg_no_user


def test_authenticate_returns_role(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "boss")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("pw1"))
    monkeypatch.setenv("USER_USERNAME", "helper")
    monkeypatch.setenv("USER_PASSWORD_HASH", auth.hash_password("pw2"))
    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()
    admin, _ = auth.authenticate("boss", "pw1", "10.0.0.3")
    helper, _ = auth.authenticate("helper", "pw2", "10.0.0.4")
    assert admin.is_admin and not helper.is_admin
