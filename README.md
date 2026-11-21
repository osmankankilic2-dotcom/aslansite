# ASLAN SITE TTS Dağıtım Paketi
# ASLAN Technologies - TTS Sistem Distribüsi

## İçerik Listesi

- `server.py` - Standalone Flask sunucu (frontend + API endpoints)
- `index.html` - TTS ve AI asistanı arayüzü
- `css/style.css` - Tasarım stilleri
- `js/script.js` - Arayüz mantığı
- `requirements.txt` - Gerekli bağımlılıklar
- `start.sh` - Sunucuyu başlatma scripti

## Kullanım

```bash
# Sunucuyu başlat
./start.sh

# Ya da manuel olarak
python3 server.py
```

## API Endpoint'lar

- `GET /` - Frontend (index.html)
- `POST /api/tts` - Metni sesli dönüştür (Edge TTS - tr-TR-AhmetNeural veya tr-TR-EmelNeural)
- `POST /api/ai` - Aslan AI asistanı (Gemini API)
- `GET /api/ai/quota` - Günlük kullanım limiti

## Depolama

Paketinizi `aslansite/` dizininde bulabilirsiniz. Bu paketi başka bir sunucuda da çalıştırabilirsiniz.

## Notlar

- `GEMINI_API_KEY` ortam değişkeni AI asistanı için zorunludur
- `edge-tts` kütüphanesi internet bağlantısı gerektirir
- Sunucu 5000 portda çalışır
