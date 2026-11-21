"""
Cerez onayi ve cihaz parmak izi (browser fingerprinting) yardimcilari.

Bu modul, kullanicinin tarayici oturumunu ve kullanim tercihlerini korumak
icin kalici bir "cihaz kimligi" uretir. Bu kimlik sayfa yenilendiginde,
tarayici yeniden acildiginda, cerez onayi reddedildiginde veya IP adresi
degistiginde degismez. Boylece gunluk kullanim haklari kalici olarak takip
edilebilir.
"""
import os
import re
import json
import time
import hashlib

from .config import CONSENT_DIR


# ------------------------------------------------------------------ yardimcilar
def sha256(text) -> str:
    """Verilen metnin SHA-256 hex ozetini dondurur."""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def safe_name(name) -> str:
    """Dosya sistemi icin guvenli dosya adi uretir."""
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", str(name or "x"))[:240]


# ------------------------------------------------------------------ cihaz kimligi
def device_identifier(provided: str = "", extra_seed: str = "",
                      stored_id: str = "") -> str:
    """
    Kalici cihaz kimligi uretir.

    Oncelik sirasi:
      1) Istemciden gelen X-Device-Id basligi (tarayicinin kalici kimligi) ->
         BU HER ZAMAN BIRINCIL KAYNAKTIR; sunucu/instance degisse de ayni kalir.
      2) Sunucuda saklanmis cihaz id'si (cerez onayi kaydi ile eslesir).
      3) Ek tohum (user-agent vb.) ve is makinesi kimligi.

    Donen deger her zaman ayni girdiler icin aynidir; boylece kullanici
    haklari sayfa yenilemelerinde ve tarayici oturumlari arasinda sifirlanmaz.
    """
    if provided:
        return sha256("aslan-client|" + str(provided))

    parts = []
    if stored_id:
        parts.append("stored:" + str(stored_id))
    parts.append("seed:" + str(extra_seed or ""))
    parts.append("host:" + _host_token())
    return sha256("aslan-device|" + "|".join(parts))


def _host_token() -> str:
    """Sunucu tarafinda kararli bir tohum okur veya yoksa olusturur."""
    global _HOST_TOKEN
    if _HOST_TOKEN:
        return _HOST_TOKEN

    token_path = os.path.join(CONSENT_DIR, ".host_token")
    try:
        if os.path.isfile(token_path):
            with open(token_path, "r", encoding="utf-8") as f:
                existing = f.read().strip()
            if existing:
                _HOST_TOKEN = existing
                return _HOST_TOKEN
    except Exception:
        pass

    try:
        os.makedirs(CONSENT_DIR, exist_ok=True)
        token = sha256(os.urandom(32))
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(token)
        _HOST_TOKEN = token
    except Exception:
        _HOST_TOKEN = sha256(os.path.join(CONSENT_DIR, str(time.time())))
    return _HOST_TOKEN


_HOST_TOKEN = ""


def fingerprint_seed(user_agent: str = "", extra: str = "") -> str:
    """Tarayici parmak izi tohumu (istemci ve sunucu tarafi ortak kullanir)."""
    return sha256((user_agent or "") + "|" + (extra or ""))


# ------------------------------------------------------------------ cerez onayi
def store_consent(device_id: str, consent: str, ip: str = "") -> dict:
    """Cerez onayi tercihini kalici olarak saklar."""
    payload = {
        "device_id": device_id,
        "consent": consent,
        "ip": ip,
        "timestamp": time.time(),
    }
    try:
        os.makedirs(CONSENT_DIR, exist_ok=True)
        path = os.path.join(CONSENT_DIR, safe_name(device_id) + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass
    return payload


def get_consent(device_id: str) -> dict:
    """Saklanmis cerez onayini okur; yoksa bos sozluk dondurur."""
    try:
        path = os.path.join(CONSENT_DIR, safe_name(device_id) + ".json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def consent_state(device_id: str) -> str:
    """'accepted' | 'rejected' | 'unknown'."""
    return get_consent(device_id).get("consent", "unknown")
