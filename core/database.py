"""
Kalici kullanici, oturum, cerez tercihi ve hesap bazli kota veritabani.

* Yerel Termux/Flask ortaminda kalici SQLite3 (<DATA_DIR>/aslan_users.db) kullanir.
* Vercel Serverless ortaminda her kullanici kaydini SHA-256 CTR + HMAC-SHA256
  (Encrypt-then-MAC) ile sifreleyerek Vercel Blob deposunda da kalici saklar.
  Boylece sayfa yenilendiginde, farkli tarayicidan girildiginde veya cerezler
  reddedildiginde kullanici verileri ve haklari asla kaybolmaz.
"""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from . import blob_store, config as cfg

_DB_LOCK = threading.RLock()

DAY = 86400
WEEK = 7 * DAY
MONTH = 30 * DAY


def _data_dir() -> str:
    if os.environ.get("ASLAN_DATA_DIR"):
        return os.environ["ASLAN_DATA_DIR"]
    if os.environ.get("ASLAN_TMP_DIR"):
        return os.path.join(os.environ["ASLAN_TMP_DIR"], "aslan_data")
    return cfg.DATA_DIR


def _db_path() -> str:
    d = _data_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "aslan_users.db")


def _blob_sync_enabled() -> bool:
    if os.environ.get("ASLAN_DISABLE_BLOB_DB") == "1":
        return False
    return blob_store.blob_enabled()


def _master_secret() -> bytes:
    secret = (
        os.environ.get("QUOTA_SIGNING_SECRET")
        or os.environ.get("BLOB_READ_WRITE_TOKEN")
        or os.environ.get("GEMINI_API_KEY")
        or "aslan-local-db-secret"
    ).strip()
    return secret.encode("utf-8")


def _enc_key() -> bytes:
    return hashlib.sha256(b"aslan-db-enc|" + _master_secret()).digest()


def _mac_key() -> bytes:
    return hashlib.sha256(b"aslan-db-mac|" + _master_secret()).digest()


def _xor_ctr(key: bytes, iv: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    offset = 0
    counter = 0
    while offset < len(data):
        stream = hashlib.sha256(key + iv + counter.to_bytes(4, "big")).digest()
        chunk = data[offset:offset + 32]
        for i, b in enumerate(chunk):
            out[offset + i] = b ^ stream[i]
        offset += 32
        counter += 1
    return bytes(out)


def encrypt_record(obj: dict) -> bytes:
    """Kullanici kaydini Encrypt-then-MAC (SHA256-CTR + HMAC-SHA256) ile sifreler."""
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    iv = os.urandom(16)
    ct = _xor_ctr(_enc_key(), iv, raw)
    header = b"ASLANDB1"
    mac = hmac.new(_mac_key(), header + iv + ct, hashlib.sha256).digest()
    return header + iv + ct + mac


def decrypt_record(blob: bytes):
    """Sifreli kaydi dogrulayip cozer; gecersizse None dondurur."""
    if not blob or len(blob) < 8 + 16 + 32 or not blob.startswith(b"ASLANDB1"):
        return None
    iv = blob[8:24]
    ct = blob[24:-32]
    mac = blob[-32:]
    expected = hmac.new(_mac_key(), b"ASLANDB1" + iv + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        plain = _xor_ctr(_enc_key(), iv, ct)
        data = json.loads(plain.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _user_tag(username_lower: str) -> str:
    return hmac.new(_mac_key(), str(username_lower).encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def _user_blob_pathname(username_lower: str) -> str:
    return "aslan-db/u/%s.enc" % _user_tag(username_lower)


def _user_blob_prefix(username_lower: str) -> str:
    return "aslan-db/u/%s/" % _user_tag(username_lower)


def _user_blob_init_path(username_lower: str) -> str:
    return "%sv0000000000.enc" % _user_blob_prefix(username_lower)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            username_lower TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            ver INTEGER NOT NULL DEFAULT 0,
            quota_json TEXT NOT NULL,
            sessions_json TEXT NOT NULL,
            wa_bot_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    try:
        conn.execute("ALTER TABLE users ADD COLUMN ver INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN wa_bot_json TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS consents (
            subject_id TEXT PRIMARY KEY,
            consent TEXT NOT NULL,
            prefs_json TEXT NOT NULL,
            ip_hash TEXT,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _row_to_dict(row) -> dict:
    keys = row.keys()
    raw_wa = row["wa_bot_json"] if ("wa_bot_json" in keys and row["wa_bot_json"]) else "{}"
    try:
        wa_bot = json.loads(raw_wa) if isinstance(raw_wa, str) else {}
    except Exception:
        wa_bot = {}
    return {
        "username": row["username"],
        "username_lower": row["username_lower"],
        "password_hash": row["password_hash"],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "_ver": int(row["ver"]) if ("ver" in keys and row["ver"] is not None) else 0,
        "quota": json.loads(row["quota_json"] or "{}"),
        "sessions": json.loads(row["sessions_json"] or "[]"),
        "wa_bot": wa_bot if isinstance(wa_bot, dict) else {},
    }


def _upsert_sqlite(conn: sqlite3.Connection, rec: dict) -> None:
    conn.execute(
        """
        INSERT INTO users (username, username_lower, password_hash, created_at, updated_at, ver, quota_json, sessions_json, wa_bot_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(username_lower) DO UPDATE SET
            username=excluded.username,
            password_hash=excluded.password_hash,
            updated_at=excluded.updated_at,
            ver=excluded.ver,
            quota_json=excluded.quota_json,
            sessions_json=excluded.sessions_json,
            wa_bot_json=excluded.wa_bot_json
        """,
        (
            rec["username"],
            rec["username_lower"],
            rec["password_hash"],
            float(rec["created_at"]),
            float(rec["updated_at"]),
            int(rec.get("_ver") or 0),
            json.dumps(rec.get("quota") or {}, ensure_ascii=False),
            json.dumps(rec.get("sessions") or [], ensure_ascii=False),
            json.dumps(rec.get("wa_bot") or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


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


def _initial_quota(now: float) -> dict:
    return {
        "ai": {"day": {"start": now, "count": 0}},
        "image": {
            "month": {"month": _current_month_key(now), "start": now, "count": 0},
        },
    }


def _rec_version_key(rec: dict) -> tuple:
    try:
        return (int(rec.get("_ver") or 0), float(rec.get("updated_at") or 0.0))
    except (TypeError, ValueError):
        return (0, 0.0)


def get_user_by_username(username: str):
    """Kullanici kaydini SQLite ve (etkinse) surumlu Vercel Blob deposundan getirir."""
    uname_lower = str(username or "").strip().casefold()
    if not uname_lower:
        return None

    with _DB_LOCK:
        local_rec = None
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username_lower = ?", (uname_lower,)
            ).fetchone()
            if row:
                local_rec = _row_to_dict(row)

            if _blob_sync_enabled():
                prefix = _user_blob_prefix(uname_lower)
                remote_rec = None
                blobs = blob_store.list_blobs(prefix, limit=100)
                candidates = [
                    b for b in blobs
                    if str(b.get("pathname", "")).startswith(prefix)
                    and str(b.get("pathname", "")).endswith(".enc")
                    and (b.get("url") or b.get("downloadUrl"))
                ]
                candidates.sort(key=lambda b: str(b.get("pathname", "")), reverse=True)
                for item in candidates:
                    url = item.get("url") or item.get("downloadUrl") or ""
                    raw_blob = blob_store.fetch_url_bytes(url)
                    if not raw_blob:
                        continue
                    parsed = decrypt_record(raw_blob)
                    if parsed and parsed.get("username_lower") == uname_lower:
                        remote_rec = parsed
                        break

                if remote_rec is None:
                    legacy_blob = blob_store.get_blob_bytes(_user_blob_pathname(uname_lower))
                    if legacy_blob:
                        parsed_legacy = decrypt_record(legacy_blob)
                        if parsed_legacy and parsed_legacy.get("username_lower") == uname_lower:
                            remote_rec = parsed_legacy

                if remote_rec is not None:
                    if (not local_rec) or _rec_version_key(remote_rec) >= _rec_version_key(local_rec):
                        _upsert_sqlite(conn, remote_rec)
                        return remote_rec
        return local_rec


def create_user(username: str, password_hash: str, initial_sessions=None):
    """
    Yeni kullanici olusturur (kullanici adi benzersizdir).

    Returns: (True, user_dict) veya (False, error_message)
    """
    clean_username = str(username or "").strip()
    uname_lower = clean_username.casefold()
    if not uname_lower:
        return False, "Kullanıcı adı gerekli."

    with _DB_LOCK:
        if get_user_by_username(uname_lower):
            return False, "Bu kullanıcı adı zaten kayıtlı. Lütfen başka bir kullanıcı adı seçin."

        now = time.time()
        record = {
            "username": clean_username,
            "username_lower": uname_lower,
            "password_hash": password_hash,
            "created_at": now,
            "updated_at": now,
            "_ver": 0,
            "quota": _initial_quota(now),
            "sessions": list(initial_sessions or []),
            "wa_bot": {},
        }

        if _blob_sync_enabled():
            ok, reason = blob_store.put_exact_bytes(
                _user_blob_init_path(uname_lower),
                encrypt_record(record),
                mime_type="application/octet-stream",
                allow_overwrite=False,
            )
            if not ok:
                if reason == "already_exists":
                    return False, "Bu kullanıcı adı zaten kayıtlı. Lütfen başka bir kullanıcı adı seçin."
                return False, "Kullanıcı kaydı uzak depoya yazılamadı. Lütfen tekrar deneyin."

        try:
            with _connect() as conn:
                _upsert_sqlite(conn, record)
        except sqlite3.IntegrityError:
            return False, "Bu kullanıcı adı zaten kayıtlı."

        return True, record


def save_user_record(record: dict) -> bool:
    """Kullanici kaydini yerel SQLite ve (etkinse) yeni surumlu Vercel Blob'a kaydeder."""
    if not isinstance(record, dict) or not record.get("username_lower"):
        return False
    uname_lower = str(record["username_lower"]).strip().casefold()
    with _DB_LOCK:
        now = time.time()
        ver = int(record.get("_ver") or 0) + 1
        record["_ver"] = ver
        record["updated_at"] = now
        with _connect() as conn:
            _upsert_sqlite(conn, record)
        if _blob_sync_enabled():
            prefix = _user_blob_prefix(uname_lower)
            init_path = _user_blob_init_path(uname_lower)
            new_path = "%sv%010d_%013d_%s.enc" % (prefix, ver, int(now * 1000), os.urandom(3).hex())
            ok, _ = blob_store.put_exact_bytes(
                new_path,
                encrypt_record(record),
                mime_type="application/octet-stream",
                allow_overwrite=False,
            )
            if ok:
                try:
                    blobs = blob_store.list_blobs(prefix, limit=100)
                    stale_urls = [
                        b.get("url") for b in blobs
                        if b.get("url")
                        and str(b.get("pathname", "")).startswith(prefix)
                        and b.get("pathname") not in (init_path, new_path)
                        and str(b.get("pathname", "")) < new_path
                    ]
                    if stale_urls:
                        blob_store.delete_blob_urls(stale_urls)
                except Exception:
                    pass
            return ok
        return True


def delete_user_blobs(username: str) -> bool:
    """Test veya hesap silme amacli kullanicinin tum Vercel Blob ve yerel kayitlarini siler."""
    uname_lower = str(username or "").strip().casefold()
    if not uname_lower:
        return False
    ok = True
    with _DB_LOCK:
        try:
            with _connect() as conn:
                conn.execute("DELETE FROM users WHERE username_lower = ?", (uname_lower,))
                conn.commit()
        except Exception:
            pass
        if _blob_sync_enabled():
            p_ok = blob_store.delete_blob_prefix(_user_blob_prefix(uname_lower))
            l_ok = blob_store.delete_blob_pathname(_user_blob_pathname(uname_lower))
            ok = bool(p_ok or l_ok)
    return ok


# ------------------------------------------------------------------ Hesap bazli kota
def _bucket(container: dict, name: str, window: int) -> dict:
    now = time.time()
    b = container.get(name)
    if not isinstance(b, dict):
        b = {"start": now, "count": 0}
    else:
        try:
            start = float(b.get("start", now))
        except (TypeError, ValueError):
            start = now
        if now - start > window:
            b = {"start": now, "count": 0}
    container[name] = b
    return b


def _ai_status_from_quota(quota_obj: dict) -> dict:
    now = time.time()
    ai_map = quota_obj.setdefault("ai", {})
    day = _bucket(ai_map, "day", DAY)
    used = max(0, int(day.get("count", 0) or 0))
    start = float(day.get("start", now))
    reset_at = start + DAY
    return {
        "limit": cfg.AI_DAILY_LIMIT,
        "used": used,
        "remaining": max(0, cfg.AI_DAILY_LIMIT - used),
        "reset_at": reset_at,
        "reset_in": max(0, int(reset_at - now)),
    }


def _image_month_bucket(quota_obj: dict, now: float = None) -> dict:
    ts = float(now if now is not None else time.time())
    cur_month = _current_month_key(ts)
    img_map = quota_obj.setdefault("image", {})
    img_map.pop("day", None)
    img_map.pop("week", None)
    bucket = img_map.get("month")
    if not isinstance(bucket, dict):
        bucket = {"month": cur_month, "start": ts, "count": 0}
    elif bucket.get("month"):
        if str(bucket.get("month")) != cur_month:
            bucket = {"month": cur_month, "start": ts, "count": 0}
    else:
        try:
            start_ts = float(bucket.get("start") or ts)
        except (TypeError, ValueError):
            start_ts = ts
        if _current_month_key(start_ts) == cur_month:
            bucket = {
                "month": cur_month,
                "start": start_ts,
                "count": max(0, int(bucket.get("count", 0) or 0)),
            }
        else:
            bucket = {"month": cur_month, "start": ts, "count": 0}
    img_map["month"] = bucket
    return bucket


def _image_status_from_quota(quota_obj: dict) -> dict:
    now = time.time()
    month = _image_month_bucket(quota_obj, now=now)
    cur_month = month.get("month") or _current_month_key(now)
    m_used = max(0, int(month.get("count", 0) or 0))
    reset_at = _next_month_reset_ts(now)
    remaining = max(0, cfg.IMAGE_MONTHLY_LIMIT - m_used)
    return {
        "month": cur_month,
        "limit": cfg.IMAGE_MONTHLY_LIMIT,
        "used": m_used,
        "remaining": remaining,
        "monthly_limit": cfg.IMAGE_MONTHLY_LIMIT,
        "monthly_used": m_used,
        "monthly_remaining": remaining,
        "allowed": m_used < cfg.IMAGE_MONTHLY_LIMIT,
        "reset_at": reset_at,
        "reset_in": max(0, int(reset_at - now)),
    }


def user_quota_snapshot(user_record: dict) -> dict:
    """Kullanici kaydindan guncel kota ozeti uretir."""
    q = dict(user_record.get("quota") or _initial_quota(time.time()))
    return {
        "ai": _ai_status_from_quota(q),
        "image": _image_status_from_quota(q),
        "account_bound": True,
        "username": user_record.get("username"),
    }


def check_user_ai_quota(username_lower: str, amount: int = 1):
    with _DB_LOCK:
        rec = get_user_by_username(username_lower)
        if not rec:
            return False, {"limit": cfg.AI_DAILY_LIMIT, "used": cfg.AI_DAILY_LIMIT, "remaining": 0, "reset_in": 0}
        q = rec.setdefault("quota", _initial_quota(time.time()))
        status = _ai_status_from_quota(q)
        return (status["used"] + amount <= cfg.AI_DAILY_LIMIT), status


def consume_user_ai_quota(username_lower: str, amount: int = 1):
    with _DB_LOCK:
        rec = get_user_by_username(username_lower)
        if not rec:
            return False, {"limit": cfg.AI_DAILY_LIMIT, "used": cfg.AI_DAILY_LIMIT, "remaining": 0, "reset_in": 0}
        q = rec.setdefault("quota", _initial_quota(time.time()))
        day = _bucket(q.setdefault("ai", {}), "day", DAY)
        used = max(0, int(day.get("count", 0) or 0))
        if used + amount > cfg.AI_DAILY_LIMIT:
            return False, _ai_status_from_quota(q)
        day["count"] = used + amount
        save_user_record(rec)
        return True, _ai_status_from_quota(q)


def check_user_image_quota(username_lower: str, amount: int = 1):
    with _DB_LOCK:
        rec = get_user_by_username(username_lower)
        if not rec:
            return False, {
                "month": _current_month_key(),
                "limit": cfg.IMAGE_MONTHLY_LIMIT,
                "used": cfg.IMAGE_MONTHLY_LIMIT,
                "remaining": 0,
                "monthly_limit": cfg.IMAGE_MONTHLY_LIMIT,
                "monthly_used": cfg.IMAGE_MONTHLY_LIMIT,
                "monthly_remaining": 0,
                "allowed": False,
                "reset_in": 0,
            }
        q = rec.setdefault("quota", _initial_quota(time.time()))
        status = _image_status_from_quota(q)
        allowed = (status["monthly_used"] + int(amount)) <= cfg.IMAGE_MONTHLY_LIMIT
        return allowed, status


def consume_user_image_quota(username_lower: str, amount: int = 1):
    with _DB_LOCK:
        rec = get_user_by_username(username_lower)
        if not rec:
            return False, {}
        q = rec.setdefault("quota", _initial_quota(time.time()))
        month = _image_month_bucket(q)
        m_u = max(0, int(month.get("count", 0) or 0))
        if m_u + int(amount) > cfg.IMAGE_MONTHLY_LIMIT:
            return False, _image_status_from_quota(q)
        month["count"] = m_u + int(amount)
        save_user_record(rec)
        return True, _image_status_from_quota(q)


# ------------------------------------------------------------------ Cerez tercihi DB
def store_consent_db(subject_id: str, consent: str, prefs: dict = None, ip: str = "") -> dict:
    now = time.time()
    ip_hash = hashlib.sha256(str(ip or "").encode("utf-8")).hexdigest()[:24]
    prefs_clean = prefs if isinstance(prefs, dict) else {}
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO consents (subject_id, consent, prefs_json, ip_hash, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    consent=excluded.consent,
                    prefs_json=excluded.prefs_json,
                    ip_hash=excluded.ip_hash,
                    updated_at=excluded.updated_at
                """,
                (str(subject_id), str(consent), json.dumps(prefs_clean), ip_hash, now),
            )
            conn.commit()
    return {"subject_id": subject_id, "consent": consent, "preferences": prefs_clean, "timestamp": now}


def get_consent_db(subject_id: str) -> dict:
    with _DB_LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM consents WHERE subject_id = ?", (str(subject_id),)
            ).fetchone()
            if not row:
                return {}
            return {
                "subject_id": row["subject_id"],
                "consent": row["consent"],
                "preferences": json.loads(row["prefs_json"] or "{}"),
                "timestamp": float(row["updated_at"]),
            }


# ------------------------------------------------------------------ WhatsApp Bot & Sifreli Anahtar Yardimcilari
def encrypt_secret_value(secret_str: str) -> str:
    """Harici API anahtari gibi hassas degerleri Encrypt-then-MAC ile base64 metne sifreler."""
    val = str(secret_str or "").strip()
    if not val:
        return ""
    return base64.b64encode(encrypt_record({"v": val})).decode("ascii")


def decrypt_secret_value(enc_str: str) -> str:
    """encrypt_secret_value ile sifrelenmis degeri guvenli sekilde cozer."""
    raw = str(enc_str or "").strip()
    if not raw:
        return ""
    try:
        blob = base64.b64decode(raw.encode("ascii"))
    except Exception:
        return ""
    obj = decrypt_record(blob)
    if isinstance(obj, dict) and isinstance(obj.get("v"), str):
        return obj["v"]
    return ""


def get_user_wa_bot(username_lower: str) -> dict:
    """Kullanicinin WhatsApp bot durum ve yapilandirmasini dondurur."""
    with _DB_LOCK:
        rec = get_user_by_username(username_lower)
        if not rec:
            return {}
        wb = rec.get("wa_bot")
        return dict(wb) if isinstance(wb, dict) else {}


def save_user_wa_bot(username_lower: str, wa_bot: dict) -> bool:
    """Kullanicinin WhatsApp bot yapilandirmasini kalici olarak kaydeder."""
    with _DB_LOCK:
        rec = get_user_by_username(username_lower)
        if not rec:
            return False
        rec["wa_bot"] = dict(wa_bot) if isinstance(wa_bot, dict) else {}
        return save_user_record(rec)
