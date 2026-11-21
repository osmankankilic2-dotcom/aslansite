"""
ASLAN Technologies - Ortak Çekirdek (core) Paketi

Bu paket hem yerel Flask sunucusu (server.py) hem de Vercel sunucusuz
fonksiyonlari (api/*.py) tarafindan paylasilan is mantigini barindirir.

Moduller:
    config.py      -> Uygulama sabitleri ve yol ayarlari
    privacy.py     -> Cerez onayi / cihaz parmak izi yardimcilari
    quota.py       -> Cihaz bazli gunluk/haftalik/aylik kota yonetimi
    media_engine.py-> Arka plan medya isleme motoru (FFmpeg)
    ai_engine.py   -> Aslan AI istemcisi, eylem yorumlayici, gorsel uretimi
"""
