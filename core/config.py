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
IMAGE_DAILY_LIMIT = 3         # gunluk gorsel uretimi hakki
IMAGE_WEEKLY_LIMIT = 5        # haftalik toplam gorsel hakki
IMAGE_MONTHLY_LIMIT = 5       # aylik toplam gorsel hakki

# Ikincil IP bazli kotu ye kullanim koruma limitleri
IP_AI_DAILY_LIMIT = 60
IP_IMAGE_DAILY_LIMIT = 30
IP_TTS_HOURLY_LIMIT = 60
IP_SOCIAL_DAILY_LIMIT = 30
IP_AUTH_HOURLY_LIMIT = 25

# ---------------------------------------------------------------- girdi & medya sinirlari
MAX_FILE_SIZE = 50 * 1024 * 1024             # 50 MB (yerel dosya siniri)
MAX_JSON_BODY_BYTES = 8 * 1024 * 1024        # 8 MB JSON govde siniri
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

# Mevcut Gemini API yapilandirmasi (son durak yedek + multimodal/gorsel)
GEMINI_TEXT_MODEL = "gemini-3.5-flash-lite"
GEMINI_LITE_MODEL = "gemini-3.5-flash-lite"
GEMINI_IMAGE_MODEL = "imagen-3.0-fast-generate-001"
GEMINI_FALLBACK_MODELS = ("gemini-3.6-flash", "gemini-3.1-flash-lite", "gemini-3.7-flash")

# Gorsel uretimi deneme sirasi
IMAGE_MODEL_CHAIN = (
    "imagen-3.0-fast-generate-001",
    "gemini-3.1-flash-lite-image",
    "gemini-nano-banana-2.1",
    "gemini-2.5-flash-image",
)
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TEXT_MODEL = "llama-3.3-70b-versatile"

# Anahtarsiz gorsel uretimi yedegi
POLLINATIONS_ENDPOINT = "https://image.pollinations.ai/prompt"

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
