"""
Cihaz bazli kullanim kotalari + HMAC-imzali HttpOnly kota cerezi + ikincil IP limiti.

Bu modul, gunluk AI mesaj hakkini ve takvim ayina bagli kati aylik (2 gorsel)
uretim hakkini tek bir kalici depoda tutar.
"""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime, timezone

from .config import (
    QUOTA_FILE,
    AI_DAILY_LIMIT,
    IMAGE_MONTHLY_LIMIT,
)

_LOCK = threading.RLock()

DAY = 86400
WEEK = 7 * DAY
MONTH = 30 * DAY
QUOTA_COOKIE_NAME = "aslan_q"


# ------------------------------------------------------------------ yardimcilar
def _safe_int(val, default: int = 0, min_val: int = 0, max_val: int = 1000) -> int:
    try:
        n = int(float(val))
    except (TypeError, ValueError):
        return default
    if n < min_val:
        return min_val
    if n > max_val:
        return max_val
    return n


def _safe_float(val, default: float) -> float:
    try:
        n = float(val)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _current_month_key(now: float = None) -> str:
    dt = datetime.fromtimestamp(float(now if now is not None else time.time()), tz=timezone.utc)
    return dt.strftime("%Y-%m")


def _next_month_reset_ts(now: float = None) -> float:
    dt = datetime.fromtimestamp(float(now if now is not None else time.time()), tz=timezone.utc)
    if dt.month == 12:
        nxt = datetime(dt.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    else:
        nxt = datetime(dt.year, dt.month + 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return nxt.timestamp()


def _signing_key() -> bytes:
    secret = (
        os.environ.get("QUOTA_SIGNING_SECRET")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("BLOB_READ_WRITE_TOKEN")
        or "aslan-local-dev-quota-key"
    ).strip()
    return hashlib.sha256(("aslan-quota-v1|" + secret).encode("utf-8")).digest()


# ------------------------------------------------------------------ depo
def _load() -> dict:
    try:
        if os.path.exists(QUOTA_FILE):
            with open(QUOTA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(QUOTA_FILE), exist_ok=True)
        tmp = QUOTA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, QUOTA_FILE)
    except Exception:
        pass


def _bucket(entry: dict, name: str, window: int) -> dict:
    """Zaman penceresi dolmus bucket'i sifirlar ve guncel bucket'i dondurur."""
    now = time.time()
    bucket = entry.get(name)
    if not isinstance(bucket, dict):
        bucket = {"start": now, "count": 0}
    elif now - _safe_float(bucket.get("start"), now) > window:
        bucket = {"start": now, "count": 0}
    entry[name] = bucket
    return bucket


def _month_bucket(img: dict, now: float = None) -> dict:
    """Takvim ayi bazli gorsel kotasi bucket'ini dondurur."""
    ts = float(now if now is not None else time.time())
    cur_month = _current_month_key(ts)
    img.pop("day", None)
    img.pop("week", None)
    bucket = img.get("month")
    if not isinstance(bucket, dict):
        bucket = {"month": cur_month, "start": ts, "count": 0}
    elif bucket.get("month"):
        if str(bucket.get("month")) != cur_month:
            bucket = {"month": cur_month, "start": ts, "count": 0}
    else:
        start_ts = _safe_float(bucket.get("start"), ts)
        if _current_month_key(start_ts) == cur_month:
            bucket = {
                "month": cur_month,
                "start": start_ts,
                "count": _safe_int(bucket.get("count", 0), 0, 0, IMAGE_MONTHLY_LIMIT * 2),
            }
        else:
            bucket = {"month": cur_month, "start": ts, "count": 0}
    img["month"] = bucket
    return bucket


def _device(store: dict, device_id: str) -> dict:
    entry = store.get(device_id)
    if not isinstance(entry, dict):
        entry = {}
        store[device_id] = entry
    entry.setdefault("ai", {})
    entry.setdefault("image", {})
    entry.setdefault("created", time.time())
    return entry


def _ai_dict_from_bucket(bucket: dict) -> dict:
    now = time.time()
    used = _safe_int(bucket.get("count", 0), 0, 0, AI_DAILY_LIMIT * 2)
    start = _safe_float(bucket.get("start"), now)
    reset_at = start + DAY
    return {
        "limit": AI_DAILY_LIMIT,
        "used": used,
        "remaining": max(0, AI_DAILY_LIMIT - used),
        "reset_at": reset_at,
        "reset_in": max(0, int(reset_at - now)),
    }


def _image_dict_from_entry(img: dict) -> dict:
    now = time.time()
    month = _month_bucket(img, now=now)
    cur_month = month.get("month") or _current_month_key(now)
    monthly_used = _safe_int(month.get("count", 0), 0, 0, IMAGE_MONTHLY_LIMIT * 2)
    reset_at = _next_month_reset_ts(now)
    remaining = max(0, IMAGE_MONTHLY_LIMIT - monthly_used)
    return {
        "month": cur_month,
        "limit": IMAGE_MONTHLY_LIMIT,
        "used": monthly_used,
        "remaining": remaining,
        "monthly_limit": IMAGE_MONTHLY_LIMIT,
        "monthly_used": monthly_used,
        "monthly_remaining": remaining,
        "allowed": monthly_used < IMAGE_MONTHLY_LIMIT,
        "reset_at": reset_at,
        "reset_in": max(0, int(reset_at - now)),
    }


# ------------------------------------------------------------------ AI mesaj kotasi
def ai_status(device_id: str) -> dict:
    """AI mesaj kotasinin guncel durumu (degistirmez)."""
    with _LOCK:
        store = _load()
        entry = _device(store, device_id)
        bucket = _bucket(entry["ai"], "day", DAY)
        return _ai_dict_from_bucket(bucket)


def check_ai(device_id: str, amount: int = 1):
    """AI mesaj hakkini harcamadan kontrol eder. (allowed, status) dondurur."""
    with _LOCK:
        store = _load()
        entry = _device(store, device_id)
        bucket = _bucket(entry["ai"], "day", DAY)
        used = _safe_int(bucket.get("count", 0), 0, 0, AI_DAILY_LIMIT * 2)
        status = _ai_dict_from_bucket(bucket)
        return (used + amount <= AI_DAILY_LIMIT), status


def consume_ai(device_id: str, amount: int = 1):
    """AI mesaj hakki harcar. (allowed, status) dondurur."""
    with _LOCK:
        store = _load()
        entry = _device(store, device_id)
        bucket = _bucket(entry["ai"], "day", DAY)
        used = _safe_int(bucket.get("count", 0), 0, 0, AI_DAILY_LIMIT * 2)
        if used + amount > AI_DAILY_LIMIT:
            return False, _ai_dict_from_bucket(bucket)
        bucket["count"] = used + amount
        entry["ai"]["day"] = bucket
        _save(store)
        return True, _ai_dict_from_bucket(bucket)


# ------------------------------------------------------------------ gorsel kotasi (aylik 2)
def image_status(device_id: str) -> dict:
    """Gorsel uretimi aylik kotasinin guncel durumu (degistirmez)."""
    with _LOCK:
        store = _load()
        entry = _device(store, device_id)
        return _image_dict_from_entry(entry["image"])


def check_image(device_id: str, amount: int = 1):
    """Aylik gorsel uretimi hakkini harcamadan kontrol eder."""
    with _LOCK:
        store = _load()
        entry = _device(store, device_id)
        status = _image_dict_from_entry(entry["image"])
        allowed = (status["monthly_used"] + int(amount)) <= IMAGE_MONTHLY_LIMIT
        return allowed, status


def consume_image(device_id: str, amount: int = 1):
    """Aylik gorsel uretimi hakki harcar."""
    with _LOCK:
        store = _load()
        entry = _device(store, device_id)
        img = entry["image"]
        month = _month_bucket(img)
        m_used = _safe_int(month.get("count", 0), 0, 0, IMAGE_MONTHLY_LIMIT * 2)
        if m_used + int(amount) > IMAGE_MONTHLY_LIMIT:
            return False, _image_dict_from_entry(img)
        month["count"] = m_used + int(amount)
        img["month"] = month
        _save(store)
        return True, _image_dict_from_entry(img)


def snapshot(device_id: str) -> dict:
    """AI + gorsel kotalarinin tek seferde ozeti."""
    return {"ai": ai_status(device_id), "image": image_status(device_id)}


# ------------------------------------------------------------------ HMAC imzali HttpOnly cerez
def encode_quota_cookie(device_id: str, snap: dict = None, secure: bool = True) -> str:
    """Cihazin kota durumunu HMAC-SHA256 ile imzali Set-Cookie degerine cevirir."""
    now = int(time.time())
    with _LOCK:
        store = _load()
        entry = _device(store, device_id)
        ai_day = _bucket(entry["ai"], "day", DAY)
        img = entry["image"]
        img_month = _month_bucket(img, now=now)

        payload = {
            "v": 2,
            "d": str(device_id)[:16],
            "au": _safe_int(ai_day.get("count", 0), 0, 0, AI_DAILY_LIMIT * 2),
            "as": int(_safe_float(ai_day.get("start"), now)),
            "im": _safe_int(img_month.get("count", 0), 0, 0, IMAGE_MONTHLY_LIMIT * 2),
            "imm": str(img_month.get("month") or _current_month_key(now)),
            "ims": int(_safe_float(img_month.get("start"), now)),
        }

    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(_signing_key(), b64.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    cookie = "%s=%s.%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (
        QUOTA_COOKIE_NAME, b64, sig, MONTH
    )
    if secure:
        cookie += "; Secure"
    return cookie


def decode_quota_cookie(device_id: str, cookie_header: str):
    """Imzali kota cerezini dogrular ve cozer; gecersizse None dondurur."""
    if not cookie_header:
        return None
    token = ""
    for part in str(cookie_header).split(";"):
        part = part.strip()
        if part.startswith(QUOTA_COOKIE_NAME + "="):
            token = part[len(QUOTA_COOKIE_NAME) + 1:].strip()
            break
    if not token or "." not in token:
        return None
    try:
        b64, sig = token.rsplit(".", 1)
        expected = hmac.new(_signing_key(), b64.encode("ascii"), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(expected, sig):
            return None
        padded = b64 + "=" * (-len(b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(data, dict) or data.get("v") not in (1, 2):
            return None
        if data.get("d") != str(device_id)[:16]:
            return None
        return data
    except Exception:
        return None


def sync_from_cookie(device_id: str, cookie_header: str) -> dict:
    """Gecerli imzali cerezdeki kullanim sayaclarini sunucu deposuyla birlestirir."""
    data = decode_quota_cookie(device_id, cookie_header)
    if not data:
        return snapshot(device_id)

    now = time.time()
    cur_month = _current_month_key(now)
    with _LOCK:
        store = _load()
        entry = _device(store, device_id)

        ai_bucket = _bucket(entry["ai"], "day", DAY)
        ai_start = _safe_float(data.get("as"), 0)
        ai_count = _safe_int(data.get("au"), 0, 0, AI_DAILY_LIMIT * 2)
        if ai_start and (now - ai_start) <= DAY and ai_count > _safe_int(ai_bucket.get("count", 0)):
            ai_bucket["count"] = ai_count
            ai_bucket["start"] = ai_start

        month_bucket = _month_bucket(entry["image"], now=now)
        c_month = str(data.get("imm") or "")
        c_start = _safe_float(data.get("ims"), 0)
        c_count = _safe_int(data.get("im"), 0, 0, IMAGE_MONTHLY_LIMIT * 2)
        same_month = (c_month == cur_month) or (not c_month and c_start and _current_month_key(c_start) == cur_month)
        if same_month and c_count > _safe_int(month_bucket.get("count", 0)):
            month_bucket["count"] = c_count
            if c_start:
                month_bucket["start"] = c_start

        _save(store)
    return snapshot(device_id)


def sync_client_usage(device_id: str, client_state=None) -> dict:
    """Istemcinin kullanim sayacini sunucu ile birlestirir."""
    if not isinstance(client_state, dict):
        return snapshot(device_id)

    now = time.time()
    cur_month = _current_month_key(now)
    with _LOCK:
        store = _load()
        entry = _device(store, device_id)
        ai_state = client_state.get("ai") if isinstance(client_state.get("ai"), dict) else {}
        img_state = client_state.get("image") if isinstance(client_state.get("image"), dict) else {}

        ai_bucket = _bucket(entry["ai"], "day", DAY)
        reset_at = ai_state.get("reset_at")
        ai_expired = False
        if reset_at is not None:
            try:
                ai_expired = float(reset_at) < now
            except (TypeError, ValueError):
                ai_expired = False
        if not ai_expired:
            server_ai = _safe_int(ai_bucket.get("count", 0), 0, 0, AI_DAILY_LIMIT * 2)
            client_ai = _safe_int(ai_state.get("used", 0), 0, 0, AI_DAILY_LIMIT * 2)
            if client_ai > server_ai:
                ai_bucket["count"] = client_ai
                ai_bucket.setdefault("start", now)

        month_bucket = _month_bucket(entry["image"], now=now)
        c_month = str(img_state.get("month") or "")
        if (not c_month) or c_month == cur_month:
            server_val = _safe_int(month_bucket.get("count", 0), 0, 0, IMAGE_MONTHLY_LIMIT * 2)
            client_val = 0
            for field in ("monthly_used", "month_used", "used"):
                client_val = max(
                    client_val,
                    _safe_int(img_state.get(field, 0), 0, 0, IMAGE_MONTHLY_LIMIT * 2),
                )
            if client_val > server_val:
                month_bucket["count"] = client_val
                month_bucket.setdefault("start", now)

        _save(store)
    return snapshot(device_id)


# ------------------------------------------------------------------ ikincil IP limiti
def check_and_consume_ip(ip: str, bucket_name: str, limit: int, window: int):
    """IP bazli ikincil hiz sinirlamasi."""
    now = time.time()
    ip_hash = hashlib.sha256(str(ip or "127.0.0.1").encode("utf-8")).hexdigest()[:24]
    with _LOCK:
        store = _load()
        ip_root = store.setdefault("ip_limits", {})
        if not isinstance(ip_root, dict):
            ip_root = {}
            store["ip_limits"] = ip_root
        entry = ip_root.setdefault(ip_hash, {})
        if not isinstance(entry, dict):
            entry = {}
            ip_root[ip_hash] = entry
        bucket = _bucket(entry, str(bucket_name), window)
        used = _safe_int(bucket.get("count", 0), 0, 0, limit * 10)
        start = _safe_float(bucket.get("start"), now)
        if used + 1 > limit:
            return False, {
                "limit": limit,
                "used": used,
                "remaining": 0,
                "reset_in": max(0, int(start + window - now)),
            }
        bucket["count"] = used + 1
        _save(store)
        return True, {
            "limit": limit,
            "used": used + 1,
            "remaining": max(0, limit - (used + 1)),
            "reset_in": max(0, int(start + window - now)),
        }
