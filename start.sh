#!/bin/bash
# ASLAN SITE TTS - Başlatma Scripti
# Bu script sunucuyu otomatik olarak başlatır

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=================================================="
echo "  ASLAN SITE TTS - Başlatma Scripti"
echo "=================================================="

# Port kontrolü
PORT=${PORT:-5000}
if lsof -i :$PORT > /dev/null 2>&1; then
    echo "⚠️  Port $PORT kullanımda. Mevcut sunucu kapatılıyor..."
    fuser -k $PORT/tcp 2>/dev/null || true
    sleep 1
fi

# Gerekli paketleri kontrol et
echo "📦 Paketler kontrol ediliyor..."
if ! python3 -c "import flask, flask_cors, edge_tts, base64, uuid, pathlib, subprocess, io" 2>/dev/null; then
    echo "❌ Gerekli paketler eksik. Kurulum yapılıyor..."
    pip install -r requirements.txt
fi

# API anahtarı kontrolü
if [ -z "$GEMINI_API_KEY" ]; then
    echo "⚠️  GEMINI_API_KEY ortam değişkeni ayarlanmamış."
    echo "   AI asistan Limited modda çalışacak."
fi

# Sunucuyu başlat
echo "🚀 Sunucu başlatılıyor..."
echo "   URL: http://localhost:$PORT"
echo "   Durdurmak için Ctrl+C"
echo "=================================================="

python3 server.py