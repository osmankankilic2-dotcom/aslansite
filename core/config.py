"""Uygulama genelinde paylasilan yapilandirma degerleri."""
import os
import tempfile


# ---------------------------------------------------------------- .env.local
def _load_env_file():
    """Yerel gelistirme icin .env.local dosyasini yukler (Vercel'de gerekmez)."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), '.env.local')
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_env_file()

# ---------------------------------------------------------------- yollar
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
TMP_ROOT = os.environ.get("ASLAN_TMP_DIR") or tempfile.gettempdir()
MEDIA_TEMP_DIR = os.path.join(TMP_ROOT, "aslan_media")
QUOTA_FILE = os.path.join(TMP_ROOT, "aslan_quota_store.json")
CONSENT_DIR = os.path.join(TMP_ROOT, "aslan_consents")

if os.environ.get("ASLAN_DATA_DIR"):
    DATA_DIR = os.environ["ASLAN_DATA_DIR"]
elif os.environ.get("VERCEL") or not os.access(BASE_DIR, os.W_OK):
    DATA_DIR = os.path.join(TMP_ROOT, "aslan_data")
else:
    DATA_DIR = os.path.join(BASE_DIR, "data")

DB_FILE = os.path.join(DATA_DIR, "aslan_users.db")

# ---------------------------------------------------------------- limitler
AI_DAILY_LIMIT = 5            # gunluk AI mesaj hakki (hesaba bagli)
IMAGE_MONTHLY_LIMIT = 2       # aylik toplam gorsel hakki (kati takvim ayi siniri)
WA_BOT_QUOTA_LIMIT = 10       # kurulum sonrasi WhatsApp bot hizmeti toplam hakki
CHAT_HISTORY_RETENTION_DAYS = 30
MAX_PINNED_CHATS = 2
MAX_SAVED_CHATS = 40
MAX_CHAT_MESSAGES = 60

# Ikincil IP bazli kotu ye kullanim koruma limitleri
IP_AI_DAILY_LIMIT = 60
IP_IMAGE_DAILY_LIMIT = 30
IP_TTS_HOURLY_LIMIT = 60
IP_STT_HOURLY_LIMIT = 60
IP_SOCIAL_DAILY_LIMIT = 30
IP_AUTH_HOURLY_LIMIT = 25
IP_MEDIA_HOURLY_LIMIT = 40
IP_WA_HOURLY_LIMIT = 60
IP_API_PER_MINUTE_LIMIT = 180
IP_SETTINGS_HOURLY_LIMIT = 40

# ---------------------------------------------------------------- girdi & medya sinirlari
MAX_FILE_SIZE = 50 * 1024 * 1024             # 50 MB (yerel dosya siniri)
MAX_JSON_BODY_BYTES = 8 * 1024 * 1024        # 8 MB JSON govde siniri
MAX_INLINE_FILES = 12                        # tek istekte en fazla inline dosya sayisi
MAX_AI_PIPELINE_STEPS = 5                    # tek AI yanitinda en fazla otonom adim
MAX_SLIDESHOW_PHOTOS = 10                    # en fazla 10 fotograf
MAX_INLINE_RESULT_BYTES = 3 * 1024 * 1024    # 3 MB: inline (data URL) sonuc siniri
MAX_HISTORY_TURNS = 10
MAX_MESSAGE_CHARS = 8000
MAX_PROMPT_CHARS = 1000
DEFAULT_VOICE = "tr-TR-AhmetNeural"
TTS_MAX_CHARS = 5000

ALLOWED_TTS_VOICES = (
    "tr-TR-AhmetNeural",
    "tr-TR-EmelNeural",
    "en-US-AndrewNeural",
    "en-US-AvaNeural",
)

ALLOWED_UPLOAD_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac",
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".3gp", ".m4v",
    ".pdf", ".txt", ".md", ".csv", ".json",
})

# ---------------------------------------------------------------- AI modeller
# Ucretsiz/anahtarsiz acik uc noktalar (oncelikli)
POLLINATIONS_TEXT_OPENAI = "https://text.pollinations.ai/openai"
POLLINATIONS_TEXT_POST = "https://text.pollinations.ai/"
POLLINATIONS_TEXT_GET = "https://text.pollinations.ai"
POLLINATIONS_TEXT_MODELS = ("openai-fast", "openai", "mistral")

DDG_STATUS_URL = "https://duckduckgo.com/duckchat/v1/status"
DDG_CHAT_URL = "https://duckduckgo.com/duckchat/v1/chat"
DDG_TEXT_MODEL = "gpt-4o-mini"

DEEPINFRA_CHAT_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
DEEPINFRA_TEXT_MODEL = "meta-llama/Llama-3.3-70B-Instruct"

HF_CHAT_URL = "https://router.huggingface.co/v1/chat/completions"
HF_TEXT_MODEL = "Qwen/Qwen2.5-72B-Instruct"

# Mevcut Gemini API yapilandirmasi (son durak yedek + multimodal/gorsel)
GEMINI_TEXT_MODEL = "gemini-4-hydrogen"
GEMINI_LITE_MODEL = "gemini-4-hydrogen"
GEMINI_IMAGE_MODEL = "imagen-3.0-fast-generate-001"
GEMINI_FALLBACK_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
)

# Gorsel uretimi deneme sirasi (Gemini yalnizca son durak yedegi)
IMAGE_MODEL_CHAIN = (
    "imagen-3.0-fast-generate-001",
    "gemini-3.1-flash-lite-image",
    "gemini-nano-banana-2.1",
    "gemini-2.5-flash-image",
)
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TEXT_MODEL = "llama-3.3-70b-versatile"

# Anahtarsiz gorsel uretimi (birincil)
POLLINATIONS_ENDPOINT = "https://image.pollinations.ai/prompt"
POLLINATIONS_IMAGE_MODELS = ("flux", "turbo", "")

# Opsiyonel harici FFmpeg / yt-dlp worker (Vercel serverless limitlerini asmak icin)
MEDIA_WORKER_URL = os.environ.get("MEDIA_WORKER_URL", "").strip().rstrip("/")
MEDIA_WORKER_TOKEN = os.environ.get("MEDIA_WORKER_TOKEN", "").strip()

# WhatsApp bot (Baileys IPv4) worker yapilandirmasi
WA_BOT_WORKER_URL = os.environ.get("WA_BOT_WORKER_URL", "").strip().rstrip("/")
WA_BOT_WORKER_TOKEN = os.environ.get("WA_BOT_WORKER_TOKEN", "").strip() or MEDIA_WORKER_TOKEN
WA_BOT_LOCAL_PORT = int(os.environ.get("WA_WORKER_PORT", "5056"))

# ---------------------------------------------------------------- cerez
COOKIE_CONSENT_KEY = "aslan_cookie_consent"

# ---------------------------------------------------------------- Vercel Blob
BLOB_API_URL = os.environ.get("VERCEL_BLOB_API_URL", "https://blob.vercel-storage.com")
BLOB_READ_WRITE_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
BLOB_STORE_ID = os.environ.get("BLOB_STORE_ID", "")

# ---------------------------------------------------------------- sosyal medya
SUPPORTED_SOCIAL_PLATFORMS = (
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "youtube.com", "youtu.be", "m.youtube.com",
    "facebook.com", "fb.watch", "fb.com",
    "instagram.com", "instagr.am",
    "twitter.com", "x.com",
    "reddit.com", "twitch.tv", "soundcloud.com",
)


def ensure_dirs() -> None:
    """Gerekli dizinleri olusturur (idempotent)."""
    for path in (MEDIA_TEMP_DIR, CONSENT_DIR, DATA_DIR):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass


ensure_dirs()
