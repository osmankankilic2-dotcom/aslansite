"""
Kullanici kayit, giris, sifre politikasi ve cerezsiz oturum dogrulama modulu.

* Kullanici adi en az 3 karakter ve benzersizdir.
* Sifre politikasi: min 12 karakter + 1 buyuk harf + 1 kucuk harf + 1 rakam + 1 nokta/ozel sembol.
* Tum sifreler PBKDF2-HMAC-SHA256 (260.000 iterasyon + rastgele salt) ile hashlenir.
* Oturumlar cerez gerektirmeyen HMAC-SHA256 imzali Bearer jetonlariyla yonetilir.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import time

from . import database

_USERNAME_RE = re.compile(r"^[\w.-]{3,32}$", re.UNICODE)
_SESSION_TTL = 30 * 86400  # 30 gun
_MAX_SESSIONS_PER_USER = 10


def _token_secret() -> bytes:
    secret = (
        os.environ.get("AUTH_SECRET")
        or os.environ.get("QUOTA_SIGNING_SECRET")
        or os.environ.get("BLOB_READ_WRITE_TOKEN")
        or os.environ.get("GEMINI_API_KEY")
        or "aslan-local-auth-secret"
    ).strip()
    return hashlib.sha256(b"aslan-auth-v1|" + secret.encode("utf-8")).digest()


# ------------------------------------------------------------------ Dogrulama
def validate_username(username: str):
    clean = str(username or "").strip()
    if len(clean) < 3:
        return False, "Kullanıcı adı en az 3 karakter olmalıdır."
    if len(clean) > 32:
        return False, "Kullanıcı adı en fazla 32 karakter olabilir."
    if not _USERNAME_RE.fullmatch(clean):
        return False, "Kullanıcı adı yalnızca harf, rakam, nokta (.), tire (-) ve alt çizgi (_) içerebilir."
    return True, clean


def validate_password_policy(password: str) -> list:
    """Sifre politikasi ihlallerini liste olarak dondurur (bos liste = gecerli)."""
    pw = str(password or "")
    errors = []
    if len(pw) < 12:
        errors.append("Şifre en az 12 karakter uzunluğunda olmalıdır.")
    if len(pw) > 128:
        errors.append("Şifre en fazla 128 karakter olabilir.")
    if not any(c.isupper() for c in pw):
        errors.append("Şifre en az 1 büyük harf (A-Z) içermelidir.")
    if not any(c.islower() for c in pw):
        errors.append("Şifre en az 1 küçük harf (a-z) içermelidir.")
    if not any(c.isdigit() for c in pw):
        errors.append("Şifre en az 1 rakam (0-9) içermelidir.")
    if not any((not c.isalnum()) and (not c.isspace()) for c in pw):
        errors.append("Şifre en az 1 nokta veya özel sembol (. ! @ # $ % _ - vb.) içermelidir.")
    return errors


def validate_registration(username: str, password: str, password_confirm: str):
    ok_u, u_or_err = validate_username(username)
    if not ok_u:
        return False, u_or_err

    pw = str(password or "")
    pw2 = str(password_confirm or "")
    if not pw or not pw2:
        return False, "Şifre ve Şifre Tekrar alanları zorunludur."
    if pw != pw2:
        return False, "Girilen şifreler birbiriyle eşleşmiyor."

    pw_errors = validate_password_policy(pw)
    if pw_errors:
        return False, " ".join(pw_errors)

    return True, u_or_err


# ------------------------------------------------------------------ Hashleme
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    iters = 260000
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, iters)
    return "pbkdf2_sha256$%d$%s$%s" % (iters, salt.hex(), dk.hex())


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        alg, iters_s, salt_hex, dk_hex = str(stored_hash).split("$", 3)
        if alg != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        calc = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, iters)
        return hmac.compare_digest(calc, expected)
    except Exception:
        return False


_DUMMY_PW_HASH = hash_password("aslan-dummy-timing-guard-!9A")


# ------------------------------------------------------------------ Cerezsiz oturum jetonu
def _sid_hash(sid: str) -> str:
    return hashlib.sha256(("aslan-sid|" + str(sid)).encode("utf-8")).hexdigest()[:32]


def _encode_token(username_lower: str, sid: str, exp: int) -> str:
    payload = json.dumps(
        {"v": 1, "u": username_lower, "sid": sid, "exp": int(exp)},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    b64 = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    sig = hmac.new(_token_secret(), b64.encode("ascii"), hashlib.sha256).hexdigest()
    return "aslan1.%s.%s" % (b64, sig)


def _decode_token(token: str):
    if not token or not str(token).startswith("aslan1."):
        return None
    parts = str(token).strip().split(".")
    if len(parts) != 3:
        return None
    _, b64, sig = parts
    expected = hmac.new(_token_secret(), b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        padded = b64 + "=" * (-len(b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(data, dict) or data.get("v") != 1:
            return None
        if float(data.get("exp", 0)) < time.time():
            return None
        if not data.get("u") or not data.get("sid"):
            return None
        return data
    except Exception:
        return None


def extract_bearer_token(headers) -> str:
    """Authorization: Bearer <token> veya X-Auth-Token basligini okur (cerezsiz)."""
    if not headers:
        return ""
    auth_val = ""
    x_val = ""
    try:
        for k, v in headers.items():
            kl = str(k).lower()
            if kl == "authorization" and v:
                auth_val = str(v).strip()
            elif kl == "x-auth-token" and v:
                x_val = str(v).strip()
    except Exception:
        pass
    if auth_val.lower().startswith("bearer "):
        return auth_val[7:].strip()
    return x_val


def issue_session_for_record(user_record: dict) -> str:
    """Kullanici kaydina yeni cerezsiz oturum ekler ve Bearer jetonu dondurur."""
    now = time.time()
    exp = int(now + _SESSION_TTL)
    sid = os.urandom(16).hex()
    sessions = [
        s for s in (user_record.get("sessions") or [])
        if isinstance(s, dict) and float(s.get("exp", 0)) > now
    ]
    sessions.append({"sid": _sid_hash(sid), "created": int(now), "exp": exp})
    if len(sessions) > _MAX_SESSIONS_PER_USER:
        sessions = sessions[-_MAX_SESSIONS_PER_USER:]
    user_record["sessions"] = sessions
    database.save_user_record(user_record)
    return _encode_token(user_record["username_lower"], sid, exp)


def authenticate_request(headers):
    """
    Istekteki cerezsiz Bearer jetonunu dogrular.

    Returns: (user_record, sid) veya (None, None)
    """
    token = extract_bearer_token(headers)
    parsed = _decode_token(token)
    if not parsed:
        return None, None
    rec = database.get_user_by_username(parsed["u"])
    if not rec:
        return None, None
    target_hash = _sid_hash(parsed["sid"])
    now = time.time()
    for s in (rec.get("sessions") or []):
        if isinstance(s, dict) and s.get("sid") == target_hash and float(s.get("exp", 0)) > now:
            return rec, parsed["sid"]
    return None, None


def register_user(username: str, password: str, password_confirm: str):
    """Yeni kullanici kaydeder (tek atomik kayit) ve oturum jetonu dondurur."""
    ok, clean_or_err = validate_registration(username, password, password_confirm)
    if not ok:
        return False, clean_or_err, 400

    pw_hash = hash_password(password)
    now = time.time()
    exp = int(now + _SESSION_TTL)
    sid = os.urandom(16).hex()
    initial_sessions = [{"sid": _sid_hash(sid), "created": int(now), "exp": exp}]

    created, rec_or_err = database.create_user(
        clean_or_err, pw_hash, initial_sessions=initial_sessions)
    if not created:
        status = 409 if "zaten kayıtlı" in str(rec_or_err).lower() else 500
        return False, rec_or_err, status

    token = _encode_token(rec_or_err["username_lower"], sid, exp)
    return True, {
        "token": token,
        "user": {"username": rec_or_err["username"]},
        "quota": database.user_quota_snapshot(rec_or_err),
    }, 201


def login_user(username: str, password: str):
    """Mevcut kullaniciyi dogrular ve oturum jetonu + kalici kotayi dondurur."""
    clean_u = str(username or "").strip()
    pw = str(password or "")
    if not clean_u or not pw:
        return False, "Kullanıcı adı ve şifre zorunludur.", 400

    rec = database.get_user_by_username(clean_u.casefold())
    if not rec:
        verify_password(pw, _DUMMY_PW_HASH)
        return False, "Kullanıcı adı veya şifre hatalı.", 401
    if not verify_password(pw, rec.get("password_hash", "")):
        return False, "Kullanıcı adı veya şifre hatalı.", 401

    token = issue_session_for_record(rec)
    return True, {
        "token": token,
        "user": {"username": rec["username"]},
        "quota": database.user_quota_snapshot(rec),
    }, 200


def logout_request(headers) -> bool:
    """Istekteki aktif oturumu veritabanindan iptal eder."""
    rec, sid = authenticate_request(headers)
    if not rec or not sid:
        return False
    target_hash = _sid_hash(sid)
    rec["sessions"] = [
        s for s in (rec.get("sessions") or [])
        if isinstance(s, dict) and s.get("sid") != target_hash
    ]
    database.save_user_record(rec)
    return True
