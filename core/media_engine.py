"""
Arka plan medya isleme motoru (Termux + Vercel tam uyumlu).

19 temel ve gelismis FFmpeg islemini destekler:
  merge_video_image, crop_image, trim_video, extract_audio,
  combine_audio_video, overlay_audio, stitch_media, slideshow_with_music,
  video_concat, merge_audio, denoise_audio, remove_background,
  extract_thumbnail, optimize_video, convert, resize,
  compress_audio, mute_video, change_speed
"""
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from . import api_utils
from .config import MEDIA_TEMP_DIR, MAX_SLIDESHOW_PHOTOS

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tif', '.tiff')
AUDIO_EXTS = ('.mp3', '.wav', '.m4a', '.aac', '.ogg', '.opus', '.flac', '.webm')
VIDEO_EXTS = ('.mp4', '.mov', '.mkv', '.webm', '.avi', '.3gp', '.m4v')

_TIMESTAMP_RE = re.compile(r'^(?:\d+(?:\.\d+)?|\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)$')

SUPPORTED_MEDIA_OPERATIONS = (
    'merge_video_image', 'crop_image', 'trim_video', 'extract_audio',
    'combine_audio_video', 'stitch_media', 'overlay_audio', 'convert', 'resize',
    'slideshow_with_music', 'video_concat', 'merge_audio', 'denoise_audio',
    'remove_background', 'extract_thumbnail', 'optimize_video',
    'compress_audio', 'mute_video', 'change_speed',
    'image_audio_video', 'add_text', 'burn_subtitles', 'apply_filter',
)

OPERATION_LABELS = {
    'merge_video_image': 'Videoya görsel ekleme',
    'crop_image': 'Görsel kırpma',
    'trim_video': 'Video kırpma',
    'extract_audio': 'Videodan ses çıkarma',
    'combine_audio_video': 'Ses + video birleştirme',
    'overlay_audio': 'Videoya ses ekleme',
    'stitch_media': 'Fotoğraf/video birleştirme',
    'slideshow_with_music': 'Fotoğraf + müzik slaytı',
    'video_concat': 'Video birleştirme',
    'merge_audio': 'Ses birleştirme',
    'denoise_audio': 'Ses arka plan temizleme',
    'remove_background': 'Arka plan kaldırma',
    'extract_thumbnail': 'Kapak kare çıkarma',
    'optimize_video': 'Video sıkıştırma',
    'convert': 'Format dönüştürme',
    'resize': 'Yeniden boyutlandırma',
    'compress_audio': 'Ses sıkıştırma',
    'mute_video': 'Videoyu sessize alma',
    'change_speed': 'Oynatma hızı değiştirme',
    'image_audio_video': 'Görsel + ses video oluşturma',
    'add_text': 'Metin ekleme',
    'burn_subtitles': 'Altyazı ekleme',
    'apply_filter': 'Görsel/video filtreleme',
    'tts': 'Metinden sese',
    'tts_control': 'Ses kontrolü',
    'social_download': 'Sosyal medya video indirme',
    'generate_image': 'Görsel üretimi',
}

_FFMPEG = None
_FFPROBE = None
_BINARIES_READY = False

OPERATION_OUTPUT_EXT = {
    'merge_video_image': '.mp4',
    'crop_image': '.jpg',
    'trim_video': '.mp4',
    'extract_audio': '.mp3',
    'combine_audio_video': '.mp4',
    'overlay_audio': '.mp4',
    'stitch_media': '.mp4',
    'slideshow_with_music': '.mp4',
    'video_concat': '.mp4',
    'merge_audio': '.mp3',
    'denoise_audio': '.mp3',
    'remove_background': '.png',
    'extract_thumbnail': '.jpg',
    'optimize_video': '.mp4',
    'convert': '.mp4',
    'resize': '.mp4',
    'compress_audio': '.mp3',
    'mute_video': '.mp4',
    'change_speed': '.mp4',
    'image_audio_video': '.mp4',
    'add_text': '.mp4',
    'burn_subtitles': '.mp4',
    'apply_filter': '.mp4',
}

CONVERT_EXT_MAP = {
    'mp4': '.mp4', 'webm': '.webm', 'mkv': '.mkv', 'gif': '.gif', 'mp3': '.mp3',
    'wav': '.wav', 'aac': '.aac', 'm4a': '.m4a', 'mov': '.mov', '3gp': '.3gp',
    'ogg': '.ogg', 'flac': '.flac', 'png': '.png', 'jpg': '.jpg', 'jpeg': '.jpg',
    'webp': '.webp',
}

AUDIO_EXT_MAP = {'mp3': '.mp3', 'wav': '.wav', 'aac': '.aac', 'm4a': '.m4a',
                 'ogg': '.ogg', 'opus': '.opus', 'flac': '.flac'}

MIME_BY_EXT = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
    '.webm': 'video/webm', '.mkv': 'video/x-matroska', '.3gp': 'video/3gpp',
    '.gif': 'image/gif', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.png': 'image/png', '.webp': 'image/webp', '.bmp': 'image/bmp',
    '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.aac': 'audio/aac',
    '.m4a': 'audio/mp4', '.ogg': 'audio/ogg', '.opus': 'audio/opus',
    '.flac': 'audio/flac', '.txt': 'text/plain', '.pdf': 'application/pdf',
}


def guess_mime(path):
    """Dosya uzantisindan guvenilir MIME turu dondurur."""
    ext = Path(str(path)).suffix.lower()
    if ext in MIME_BY_EXT:
        return MIME_BY_EXT[ext]
    import mimetypes
    return mimetypes.guess_type(str(path))[0] or 'application/octet-stream'


# ===================================================================== ikili dosyalar
def _ensure_binaries() -> None:
    """ffmpeg / ffprobe ikili dosyalarini bulur (gerekirse paketlerden yukler)."""
    global _FFMPEG, _FFPROBE, _BINARIES_READY
    if _BINARIES_READY:
        return
    _BINARIES_READY = True

    _FFMPEG = shutil.which('ffmpeg')
    _FFPROBE = shutil.which('ffprobe')

    if not _FFMPEG:
        try:
            import imageio_ffmpeg
            _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            _FFMPEG = None

    if not _FFPROBE and _FFMPEG:
        sibling = str(Path(_FFMPEG).with_name('ffprobe'))
        if os.path.exists(sibling):
            _FFPROBE = sibling

    if not _FFMPEG:
        try:
            from static_ffmpeg import static_ffmpeg
            static_ffmpeg.add_paths()
            _FFMPEG = shutil.which('ffmpeg')
            _FFPROBE = shutil.which('ffprobe')
        except Exception:
            pass


def ffmpeg_path() -> str:
    _ensure_binaries()
    return _FFMPEG or ''


def ffprobe_path() -> str:
    _ensure_binaries()
    return _FFPROBE or ''


def ffmpeg_available() -> bool:
    return bool(ffmpeg_path())


# ===================================================================== calistirma
def run_ffmpeg(args, input_files=None, output_file=None, timeout=None, input_options=None):
    """ffmpeg komutunu calistirir. (basarili_mi, cikti_or_hata) dondurur."""
    if not ffmpeg_available():
        return False, "Aslan AI medya isleme servisi su an kullanilamiyor."
    if timeout is None:
        timeout = 50 if os.environ.get('VERCEL') else 300
    cmd = [ffmpeg_path(), '-y', '-hide_banner', '-loglevel', 'error', '-threads', '2']
    for idx, f in enumerate(input_files or []):
        if input_options and idx < len(input_options) and input_options[idx]:
            cmd.extend([str(o) for o in input_options[idx]])
        cmd.extend(['-i', str(f)])
    cmd.extend([str(a) for a in args])
    if output_file:
        cmd.append(str(output_file))
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            err = result.stderr.decode('utf-8', errors='replace').strip()
            return False, api_utils.safe_error_message(err, 'Bilinmeyen medya hatasi')
        return True, output_file
    except subprocess.TimeoutExpired:
        return False, "Medya isleme zaman asimi (sunucu sure sinirini asti)"
    except Exception as e:
        return False, api_utils.safe_error_message(e)


def get_media_info(filepath):
    """Dosya suresi/akislari bilgisini dondurur (ffprobe yoksa ffmpeg ile cozumler)."""
    path = str(filepath)
    probe = ffprobe_path()
    if probe:
        try:
            cmd = [probe, '-v', 'quiet', '-print_format', 'json',
                   '-show_format', '-show_streams', path]
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode == 0:
                return json.loads(result.stdout.decode('utf-8'))
        except Exception:
            pass
    return _probe_with_ffmpeg(path)


def _probe_with_ffmpeg(path):
    """ffprobe yoksa 'ffmpeg -i' ciktisindan ozet bilgi cikarir."""
    if not ffmpeg_available():
        return None
    try:
        result = subprocess.run([ffmpeg_path(), '-hide_banner', '-i', path],
                                capture_output=True, timeout=30)
        text = result.stderr.decode('utf-8', errors='replace')
    except Exception:
        return None

    streams = []
    duration = 0.0
    for line in text.splitlines():
        line = line.strip()
        m = re.search(r'Stream #\d+:\d+.*?: (Video|Audio): (\w+)', line)
        if m:
            streams.append({'codec_type': m.group(1).lower(), 'codec_name': m.group(2)})
            continue
        m = re.search(r'Duration: (\d+):(\d+):(\d+\.\d+)', line)
        if m:
            duration = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)))
    size = 0
    try:
        size = os.path.getsize(path)
    except Exception:
        pass
    if not streams and not duration:
        return None
    return {'streams': streams, 'format': {'duration': str(duration), 'size': str(size)}}


def media_summary(filepath):
    """Kullaniciya gosterilecek ozet medya bilgisi."""
    info = get_media_info(filepath) or {}
    streams = info.get('streams', []) or []
    fmt = info.get('format', {}) or {}
    try:
        duration = float(fmt.get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0.0
    types = [s.get('codec_type') for s in streams]
    try:
        size = int(fmt.get('size') or os.path.getsize(filepath))
    except Exception:
        size = 0
    ext = Path(filepath).suffix.lower()
    kind = 'image'
    if ext in AUDIO_EXTS or ('audio' in types and 'video' not in types):
        kind = 'audio'
    elif ext in VIDEO_EXTS or 'video' in types:
        kind = 'video'
    return {
        'kind': kind,
        'duration': round(duration, 2),
        'size': size,
        'has_audio': 'audio' in types,
        'has_video': 'video' in types,
        'codecs': [s.get('codec_name') for s in streams if s.get('codec_name')],
    }


# ===================================================================== dogrulama yardimcilari
def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_even(value, default, lo=16, hi=3840):
    v = max(lo, min(_as_int(value, default), hi))
    v = v - (v % 2)
    return max(lo, v)


def _validate_timestamp(val):
    s = str(val or '').strip()
    return bool(s and _TIMESTAMP_RE.fullmatch(s))


def execute_media_operation(operation, input_files, output_path, params=None):
    """Belirtilen medya islemini gerceklestirir."""
    params = params or {}
    input_files = [str(f) for f in (input_files or [])]
    output_path = Path(output_path)
    operation = str(operation or '').strip()

    if operation == 'merge_video_image':
        return _op_merge_video_image(input_files, output_path, params)
    if operation == 'crop_image':
        return _op_crop_image(input_files, output_path, params)
    if operation == 'trim_video':
        return _op_trim_video(input_files, output_path, params)
    if operation == 'extract_audio':
        return _op_extract_audio(input_files, output_path, params)
    if operation == 'combine_audio_video':
        return _op_combine_audio_video(input_files, output_path)
    if operation == 'overlay_audio':
        return _op_overlay_audio(input_files, output_path)
    if operation == 'stitch_media':
        return _op_stitch_media(input_files, output_path, params)
    if operation == 'slideshow_with_music':
        return _op_slideshow_with_music(input_files, output_path, params)
    if operation == 'video_concat':
        return _op_video_concat(input_files, output_path, params)
    if operation == 'merge_audio':
        return _op_merge_audio(input_files, output_path, params)
    if operation == 'denoise_audio':
        return _op_denoise_audio(input_files, output_path, params)
    if operation == 'remove_background':
        return _op_remove_background(input_files, output_path, params)
    if operation == 'extract_thumbnail':
        return _op_extract_thumbnail(input_files, output_path, params)
    if operation == 'optimize_video':
        return _op_optimize_video(input_files, output_path, params)
    if operation == 'convert':
        return _op_convert(input_files, output_path, params)
    if operation == 'resize':
        return _op_resize(input_files, output_path, params)
    if operation == 'compress_audio':
        return _op_compress_audio(input_files, output_path, params)
    if operation == 'mute_video':
        return _op_mute_video(input_files, output_path)
    if operation == 'change_speed':
        return _op_change_speed(input_files, output_path, params)
    if operation == 'image_audio_video':
        return _op_image_audio_video(input_files, output_path, params)
    if operation == 'add_text':
        return _op_add_text(input_files, output_path, params)
    if operation == 'burn_subtitles':
        return _op_burn_subtitles(input_files, output_path, params)
    if operation == 'apply_filter':
        return _op_apply_filter(input_files, output_path, params)
    return False, "Bilinmeyen islem: %s" % operation


# ---------------------------------------------------------------- video + gorsel
def _op_merge_video_image(input_files, output_path, params):
    if len(input_files) < 2:
        return False, "Video ve gorsel dosyasi gerekli (sira: video, gorsel)"
    pos = params.get('position', 'bottom-right')
    opacity = max(0.0, min(_as_float(params.get('opacity', 1.0), 1.0), 1.0))
    scale = max(0.05, min(_as_float(params.get('scale', 0.2), 0.2), 1.0))

    positions = {
        'top-left': '10:10',
        'top-right': 'main_w-overlay_w-10:10',
        'bottom-left': '10:main_h-overlay_h-10',
        'bottom-right': 'main_w-overlay_w-10:main_h-overlay_h-10',
        'center': '(main_w-overlay_w)/2:(main_h-overlay_h)/2',
    }
    overlay_pos = positions.get(pos, positions['bottom-right'])
    if opacity < 1.0:
        vf_img = ('[1:v]scale=iw*%s:ih*%s,format=rgba,'
                  'colorchannelmixer=aa=%s[img]' % (scale, scale, opacity))
    else:
        vf_img = '[1:v]scale=iw*%s:ih*%s[img]' % (scale, scale)

    filter_complex = ('%s;[0:v][img]overlay=%s:format=auto,format=yuv420p[v]'
                      % (vf_img, overlay_pos))
    out = output_path.with_suffix('.mp4')
    args = ['-filter_complex', filter_complex, '-map', '[v]', '-map', '0:a?',
            '-c:v', 'libx264', '-preset', 'veryfast', '-c:a', 'aac', '-shortest']
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


# ---------------------------------------------------------------- gorsel/video kirpma
def _op_crop_image(input_files, output_path, params):
    if not input_files:
        return False, "Kirpma icin dosya gerekli"
    x = max(0, min(_as_int(params.get('x', 0), 0), 8192))
    y = max(0, min(_as_int(params.get('y', 0), 0), 8192))
    w = params.get('width')
    h = params.get('height')
    if not w or not h:
        return False, "Kirpma icin genislik (width) ve yukseklik (height) gerekli"
    w, h = _as_int(w, 0), _as_int(h, 0)
    if w <= 0 or h <= 0 or w > 8192 or h > 8192:
        return False, "Genislik ve yukseklik 1-8192 araliginda olmali"

    src_ext = Path(input_files[0]).suffix.lower()
    if src_ext in IMAGE_EXTS:
        out = output_path.with_suffix('.png')
        args = ['-vf', 'crop=%d:%d:%d:%d' % (w, h, x, y), '-frames:v', '1']
    else:
        out = output_path.with_suffix('.mp4')
        args = ['-vf', 'crop=%d:%d:%d:%d' % (w, h, x, y),
                '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-c:a', 'copy']
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


# ---------------------------------------------------------------- video kirpma
def _op_trim_video(input_files, output_path, params):
    if not input_files:
        return False, "Kirpma icin video gerekli"
    start = str(params.get('start', '00:00:00')).strip()
    if not _validate_timestamp(start):
        return False, "Gecersiz baslangic zamani bicimi"

    duration = params.get('duration')
    end = params.get('end')
    args = ['-ss', start]
    if duration is not None and str(duration).strip() != '':
        if not _validate_timestamp(duration):
            return False, "Gecersiz sure bicimi"
        args.extend(['-t', str(duration).strip()])
    elif end is not None and str(end).strip() != '':
        if not _validate_timestamp(end):
            return False, "Gecersiz bitis zamani bicimi"
        args.extend(['-to', str(end).strip()])
    out = output_path.with_suffix('.mp4')
    args.extend(['-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-c:a', 'aac'])
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


# ---------------------------------------------------------------- ses cikarma
def _op_extract_audio(input_files, output_path, params):
    if not input_files:
        return False, "Ses cikarma icin dosya gerekli"
    fmt = str(params.get('format', 'mp3')).lower()
    codec_map = {'mp3': 'libmp3lame', 'wav': 'pcm_s16le', 'aac': 'aac', 'm4a': 'aac',
                 'opus': 'libopus', 'ogg': 'libvorbis', 'flac': 'flac'}
    ext_map = {'mp3': '.mp3', 'wav': '.wav', 'aac': '.aac', 'm4a': '.m4a',
               'opus': '.opus', 'ogg': '.ogg', 'flac': '.flac'}
    codec = codec_map.get(fmt, 'libmp3lame')
    out = output_path.with_suffix(ext_map.get(fmt, '.mp3'))
    args = ['-vn', '-acodec', codec, '-map', '0:a:0?']
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    if not ok:
        return False, res
    info = get_media_info(str(out))
    if info and not any(s.get('codec_type') == 'audio' for s in info.get('streams', [])):
        return False, "Kaynak dosyada cikarilabilir ses parcasi bulunamadi"
    return True, out


# ---------------------------------------------------------------- ses + video / gorsel
def _split_media_roles(input_files):
    videos, images, audios = [], [], []
    for f in input_files:
        ext = Path(f).suffix.lower()
        if ext in IMAGE_EXTS:
            images.append(f)
        elif ext in AUDIO_EXTS:
            audios.append(f)
        elif ext in VIDEO_EXTS:
            videos.append(f)
        else:
            info = get_media_info(f) or {}
            types = [s.get('codec_type') for s in info.get('streams', [])]
            if 'video' in types:
                videos.append(f)
            elif 'audio' in types:
                audios.append(f)
    return videos, images, audios


def _op_image_audio_video(input_files, output_path, params=None):
    """Tek gorsel + ses dosyasini MP4 videoya donusturur."""
    videos, images, audios = _split_media_roles(input_files)
    if images and audios:
        img_file, aud_file = images[0], audios[0]
        out = output_path.with_suffix('.mp4')
        vf = 'scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p'
        args = [
            '-vf', vf, '-c:v', 'libx264', '-tune', 'stillimage',
            '-preset', 'veryfast', '-r', '25', '-c:a', 'aac', '-b:a', '192k',
            '-shortest', '-movflags', '+faststart',
        ]
        ok, res = run_ffmpeg(
            args,
            input_files=[img_file, aud_file],
            output_file=str(out),
            input_options=[['-loop', '1', '-framerate', '25'], []],
        )
        return (True, out) if ok else (False, res)
    if videos and audios:
        return _op_combine_audio_video([videos[0], audios[0]], output_path)
    return False, "Gorsel/video ve ses dosyasi gerekli"


def _op_combine_audio_video(input_files, output_path):
    if len(input_files) < 2:
        return False, "Ses ve video dosyasi gerekli"
    videos, images, audios = _split_media_roles(input_files)
    if images and audios and not videos:
        return _op_image_audio_video([images[0], audios[0]], output_path)
    ordered = [videos[0], audios[0]] if (videos and audios) else input_files[:2]
    out = output_path.with_suffix('.mp4')
    args = ['-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-shortest', '-movflags', '+faststart']
    ok, res = run_ffmpeg(args, input_files=ordered, output_file=str(out))
    if not ok:
        args_re = ['-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264',
                   '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', '-movflags', '+faststart']
        ok, res = run_ffmpeg(args_re, input_files=ordered, output_file=str(out))
    if not ok:
        return False, res
    info = get_media_info(str(out))
    types = [s.get('codec_type') for s in info.get('streams', [])] if info else []
    if info and ('video' not in types or 'audio' not in types):
        return False, "Birlestirilen dosyada beklenen akislar yok (%s)" % types
    return True, out


def _op_overlay_audio(input_files, output_path):
    if len(input_files) < 2:
        return False, "Video/gorsel ve ses dosyasi gerekli"
    videos, images, audios = _split_media_roles(input_files)
    if images and audios and not videos:
        return _op_image_audio_video([images[0], audios[0]], output_path)
    ordered = [videos[0], audios[0]] if (videos and audios) else input_files[:2]
    out = output_path.with_suffix('.mp4')
    args = ['-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-shortest', '-movflags', '+faststart']
    ok, res = run_ffmpeg(args, input_files=ordered, output_file=str(out))
    if not ok:
        args_re = ['-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264',
                   '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', '-movflags', '+faststart']
        ok, res = run_ffmpeg(args_re, input_files=ordered, output_file=str(out))
    return (True, out) if ok else (False, res)


# ---------------------------------------------------------------- birlestirme
def _op_stitch_media(input_files, output_path, params):
    transition = str(params.get('transition', 'none')).lower()
    img_dur = max(0.5, min(_as_float(params.get('per_image_duration', 3), 3.0), 30.0))
    fade_dur = max(0.1, min(_as_float(params.get('transition_duration', 1), 1.0),
                            max(0.1, img_dur - 0.1)))

    if len(input_files) < 2:
        return False, "En az 2 dosya gerekli"

    is_image = [Path(f).suffix.lower() in IMAGE_EXTS for f in input_files]
    is_audio = [Path(f).suffix.lower() in AUDIO_EXTS for f in input_files]
    out = output_path.with_suffix('.mp4')

    if any(is_image) and any(is_audio) and all(a or b for a, b in zip(is_image, is_audio)):
        return _op_slideshow_with_music(input_files, out, params)

    if all(is_image):
        if len(input_files) > MAX_SLIDESHOW_PHOTOS:
            return False, "En fazla %d fotograf kullanilabilir" % MAX_SLIDESHOW_PHOTOS
        parts = []
        in_opts = []
        for i in range(len(input_files)):
            in_opts.append(['-loop', '1', '-t', str(img_dur), '-framerate', '30'])
            parts.append(
                '[%d:v]scale=1280:720:force_original_aspect_ratio=decrease,'
                'pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,'
                'fps=30,settb=AVTB,setpts=PTS-STARTPTS[v%d];' % (i, i))
        chain = ''.join(parts)
        if transition == 'fade' and len(input_files) > 1:
            prev = '[v0]'
            for i in range(len(input_files) - 1):
                offset = round(max(0.0, (i + 1) * (img_dur - fade_dur)), 3)
                nxt = '[x%d]' % i
                chain += ('%s[v%d]xfade=transition=fade:duration=%s:offset=%s%s;'
                          % (prev, i + 1, fade_dur, offset, nxt))
                prev = nxt
            chain += '%sformat=yuv420p[outv]' % prev
        else:
            chain += ''.join('[v%d]' % i for i in range(len(input_files)))
            chain += 'concat=n=%d:v=1:a=0[outv]' % len(input_files)
        args = ['-filter_complex', chain, '-map', '[outv]', '-r', '30',
                '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
        ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out), input_options=in_opts)
        return (True, out) if ok else (False, res)

    return _op_video_concat(input_files, out, {'quality': 'reencode'})


def _op_slideshow_with_music(input_files, output_path, params):
    """Fotograflardan + arka plan muziginden video/slayt olusturur (en fazla 10 fotograf)."""
    photos = [f for f in input_files if Path(f).suffix.lower() in IMAGE_EXTS]
    audio = None
    for f in input_files:
        if Path(f).suffix.lower() in AUDIO_EXTS:
            audio = f
            break
    if not photos:
        return False, "Slayt icin en az 1 fotograf gerekli"
    if len(photos) > MAX_SLIDESHOW_PHOTOS:
        return False, "En fazla %d fotograf kullanilabilir" % MAX_SLIDESHOW_PHOTOS

    per_image = max(1.0, min(_as_float(params.get('per_image_duration', 3), 3.0), 30.0))
    width = _clamp_even(params.get('width', 1280), 1280, lo=240, hi=3840)
    height = _clamp_even(params.get('height', 720), 720, lo=240, hi=3840)
    transition = str(params.get('transition', 'fade')).lower()
    out = output_path.with_suffix('.mp4')

    parts = []
    in_opts = []
    for i in range(len(photos)):
        in_opts.append(['-loop', '1', '-t', str(per_image), '-framerate', '30'])
        parts.append(
            '[%d:v]scale=%d:%d:force_original_aspect_ratio=decrease,'
            'pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,'
            'fps=30,settb=AVTB,setpts=PTS-STARTPTS[v%d];' % (i, width, height,
                                                            width, height, i))
    chain = ''.join(parts)
    if transition == 'fade' and len(photos) > 1:
        fade = max(0.1, min(_as_float(params.get('transition_duration', 0.6), 0.6),
                            max(0.1, per_image - 0.1)))
        prev = '[v0]'
        for i in range(len(photos) - 1):
            offset = round(max(0.0, (i + 1) * (per_image - fade)), 3)
            nxt = '[x%d]' % i
            chain += ('%s[v%d]xfade=transition=fade:duration=%s:offset=%s%s;'
                      % (prev, i + 1, fade, offset, nxt))
            prev = nxt
        last = prev
        total_dur = max(1.0, round(len(photos) * per_image - (len(photos) - 1) * fade, 3))
    else:
        chain += ''.join('[v%d]' % i for i in range(len(photos)))
        chain += 'concat=n=%d:v=1:a=0[outv]' % len(photos)
        last = '[outv]'
        total_dur = _total_duration(photos, per_image)

    if audio:
        in_opts.append([])
        fade_out_d = min(2.0, round(total_dur * 0.3, 3))
        fade_out_st = max(0.0, round(total_dur - fade_out_d, 3))
        if not chain.endswith(';'):
            chain += ';'
        chain += ('[%d:a]atrim=0:%s,asetpts=PTS-STARTPTS,'
                  'afade=t=in:st=0:d=1,afade=t=out:st=%s:d=%s[aout]'
                  % (len(photos), total_dur, fade_out_st, fade_out_d))
        args = ['-filter_complex', chain, '-map', last, '-map', '[aout]',
                '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart']
        return _run_with_audio(args, photos + [audio], out, input_options=in_opts)

    args = ['-filter_complex', chain, '-map', last, '-c:v', 'libx264',
            '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
    return _run_plain(args, photos, out, input_options=in_opts)


def _total_duration(photos, per_image):
    return max(1.0, len(photos) * per_image)


def _run_with_audio(args, inputs, output_path, input_options=None):
    ok, res = run_ffmpeg(args, input_files=inputs, output_file=str(output_path), input_options=input_options)
    return (True, output_path) if ok else (False, res)


def _run_plain(args, inputs, output_path, input_options=None):
    ok, res = run_ffmpeg(args, input_files=inputs, output_file=str(output_path), input_options=input_options)
    return (True, output_path) if ok else (False, res)


def _write_concat_list(list_file: Path, paths: list):
    """FFmpeg concat listesini guvenli kacislarla yazar (satir sonu enjeksiyonunu engeller)."""
    lines = []
    for p in paths:
        s = str(p)
        if '\n' in s or '\r' in s:
            return False, "Gecersiz dosya yolu"
        escaped = s.replace("'", "'\\''")
        lines.append("file '%s'\n" % escaped)
    with open(list_file, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    return True, None


def _op_video_concat(input_files, output_path, params):
    """Iki (veya daha fazla) videoyu birlestirir (uyumsuz kodeklerde otomatik filter_complex yedegi)."""
    videos = [f for f in input_files if Path(f).suffix.lower() in VIDEO_EXTS]
    if len(videos) < 2:
        videos = [f for f in input_files if Path(f).suffix.lower() not in IMAGE_EXTS]
    if len(videos) < 2:
        return False, "Birlestirme icin en az 2 video gerekli"

    out = output_path.with_suffix('.mp4')
    reencode = str(params.get('quality', 'copy')).lower() != 'copy'
    list_file = out.with_suffix('.concat.txt')
    ok_list, list_err = _write_concat_list(list_file, videos)
    if not ok_list:
        return False, list_err
    if reencode:
        args = ['-f', 'concat', '-safe', '0', '-i', str(list_file),
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-af', 'aresample=async=1',
                '-movflags', '+faststart']
    else:
        args = ['-f', 'concat', '-safe', '0', '-i', str(list_file),
                '-c', 'copy', '-movflags', '+faststart']
    ok, res = run_ffmpeg(args, output_file=str(out))
    try:
        if list_file.exists():
            list_file.unlink()
    except Exception:
        pass
    if ok:
        return True, out

    # Farkli cozunurluk/kodek iceren telefon videolari icin normalize edilmis concat yedegi
    has_audio = [
        any(s.get('codec_type') == 'audio' for s in (get_media_info(v) or {}).get('streams', []))
        for v in videos
    ]
    all_have_audio = all(has_audio)
    parts = []
    for i in range(len(videos)):
        parts.append(
            '[%d:v]scale=1280:720:force_original_aspect_ratio=decrease,'
            'pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v%d];' % (i, i)
        )
        if all_have_audio:
            parts.append('[%d:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a%d];' % (i, i))
    if all_have_audio:
        inputs_tag = ''.join('[v%d][a%d]' % (i, i) for i in range(len(videos)))
        fc = ''.join(parts) + '%sconcat=n=%d:v=1:a=1[outv][outa]' % (inputs_tag, len(videos))
        args_fc = ['-filter_complex', fc, '-map', '[outv]', '-map', '[outa]',
                   '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                   '-c:a', 'aac', '-movflags', '+faststart']
    else:
        inputs_tag = ''.join('[v%d]' % i for i in range(len(videos)))
        fc = ''.join(parts) + '%sconcat=n=%d:v=1:a=0[outv]' % (inputs_tag, len(videos))
        args_fc = ['-filter_complex', fc, '-map', '[outv]',
                   '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                   '-movflags', '+faststart']
    ok2, res2 = run_ffmpeg(args_fc, input_files=videos, output_file=str(out))
    return (True, out) if ok2 else (False, res2)


def _op_merge_audio(input_files, output_path, params):
    """Ses dosyalarini birlestirir (farkli formatlarda otomatik filter_complex yedegi)."""
    audios = [f for f in input_files if Path(f).suffix.lower() in AUDIO_EXTS]
    if len(audios) < 2:
        return False, "Birlestirme icin en az 2 ses dosyasi gerekli"
    fmt = str(params.get('format', 'mp3')).lower()
    codec = {'mp3': 'libmp3lame', 'wav': 'pcm_s16le', 'm4a': 'aac'}.get(fmt, 'libmp3lame')
    ext = {'mp3': '.mp3', 'wav': '.wav', 'm4a': '.m4a'}.get(fmt, '.mp3')
    out = output_path.with_suffix(ext)
    list_file = out.with_suffix('.aconcat.txt')
    ok_list, list_err = _write_concat_list(list_file, audios)
    if not ok_list:
        return False, list_err
    args = ['-f', 'concat', '-safe', '0', '-i', str(list_file),
            '-vn', '-acodec', codec]
    ok, res = run_ffmpeg(args, output_file=str(out))
    try:
        if list_file.exists():
            list_file.unlink()
    except Exception:
        pass
    if ok:
        return True, out

    fc = ''.join('[%d:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a%d];' % (i, i)
                 for i in range(len(audios)))
    fc += ''.join('[a%d]' % i for i in range(len(audios))) + 'concat=n=%d:v=0:a=1[aout]' % len(audios)
    args2 = ['-filter_complex', fc, '-map', '[aout]', '-vn', '-acodec', codec]
    ok2, res2 = run_ffmpeg(args2, input_files=audios, output_file=str(out))
    return (True, out) if ok2 else (False, res2)


def _op_denoise_audio(input_files, output_path, params):
    """Ses arka plan gurultusunu temizler."""
    if not input_files:
        return False, "Ses temizleme icin dosya gerekli"
    strength = max(0.0, min(_as_float(params.get('strength', 0.6), 0.6), 1.0))
    amount = int(6 + strength * 20)
    fmt = str(params.get('format', 'mp3')).lower()
    codec = {'mp3': 'libmp3lame', 'wav': 'pcm_s16le'}.get(fmt, 'libmp3lame')
    out = output_path.with_suffix('.mp3' if fmt == 'mp3' else '.wav')
    filters = [
        'highpass=f=80',
        'lowpass=f=14000',
        'afftdn=nr=%d:nf=-30' % amount,
        'dynaudnorm=f=150:g=5',
    ]
    args = ['-af', ','.join(filters), '-acodec', codec]
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


def _op_remove_background(input_files, output_path, params):
    """Gorselden arka plan kaldirir (seffaf PNG)."""
    if not input_files:
        return False, "Arka plan kaldirma icin gorsel gerekli"
    color = str(params.get('color', 'green')).strip().lower()
    hex_color = {'green': '0x00FF00', 'red': '0xFF0000', 'blue': '0x0000FF',
                 'white': '0xFFFFFF', 'black': '0x000000'}.get(color, '0x00FF00')
    tolerance = max(0.01, min(_as_float(params.get('tolerance', 0.18), 0.18), 0.9))
    out = output_path.with_suffix('.png')
    args = ['-vf', 'colorkey=%s:%s:0.0,format=rgba' % (hex_color, tolerance),
            '-frames:v', '1']
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


def _op_extract_thumbnail(input_files, output_path, params):
    """Videodan kapak kare (poster) cikarir."""
    if not input_files:
        return False, "Kapak kare icin video gerekli"
    at = max(0.0, min(_as_float(params.get('time', 1.0), 1.0), 86400.0))
    out = output_path.with_suffix('.jpg')
    args = ['-ss', str(at), '-frames:v', '1', '-q:v', '3']
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


def _op_optimize_video(input_files, output_path, params):
    """Videoyu paylasima uygun sekilde sikistirir."""
    if not input_files:
        return False, "Sikistirma icin video gerekli"
    max_side = _clamp_even(params.get('max_side', 1280), 1280, lo=240, hi=3840)
    crf = max(16, min(_as_int(params.get('crf', 28), 28), 40))
    out = output_path.with_suffix('.mp4')
    vf = ("scale='min(%d,iw)':'min(%d,ih)':force_original_aspect_ratio=decrease,"
          "scale=trunc(iw/2)*2:trunc(ih/2)*2" % (max_side, max_side))
    args = ['-vf', vf, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(crf),
            '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart']
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


def _op_compress_audio(input_files, output_path, params):
    """Ses dosyasini hedef bit hizina gore sikistirir."""
    if not input_files:
        return False, "Ses sikistirma icin dosya gerekli"
    bitrate = str(params.get('bitrate', '96k')).strip().lower()
    if bitrate not in ('48k', '64k', '96k', '128k', '192k'):
        bitrate = '96k'
    fmt = str(params.get('format', 'mp3')).strip().lower()
    if fmt == 'm4a':
        out = output_path.with_suffix('.m4a')
        args = ['-vn', '-c:a', 'aac', '-b:a', bitrate]
    else:
        out = output_path.with_suffix('.mp3')
        args = ['-vn', '-c:a', 'libmp3lame', '-b:a', bitrate]
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


def _op_mute_video(input_files, output_path):
    """Videodaki ses kanalini tamamen kaldirir."""
    if not input_files:
        return False, "Sessize alma icin video gerekli"
    out = output_path.with_suffix('.mp4')
    args = ['-an', '-c:v', 'copy', '-movflags', '+faststart']
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    if not ok:
        args_re = ['-an', '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
        ok, res = run_ffmpeg(args_re, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


def _op_change_speed(input_files, output_path, params):
    """Video veya ses oynatma hizini degistirir (0.5x - 2.0x)."""
    if not input_files:
        return False, "Hiz degistirme icin dosya gerekli"
    speed = max(0.5, min(_as_float(params.get('speed', 1.25), 1.25), 2.0))
    src_ext = Path(input_files[0]).suffix.lower()
    info = get_media_info(input_files[0]) or {}
    types = [s.get('codec_type') for s in info.get('streams', [])]
    has_video = ('video' in types) or (src_ext in VIDEO_EXTS)
    has_audio = 'audio' in types

    if not has_video:
        out = output_path.with_suffix('.mp3')
        args = ['-vn', '-af', 'atempo=%.3f' % speed, '-c:a', 'libmp3lame']
        ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
        return (True, out) if ok else (False, res)

    out = output_path.with_suffix('.mp4')
    v_filter = 'setpts=%.4f*PTS' % (1.0 / speed)
    if has_audio:
        args = ['-vf', v_filter, '-af', 'atempo=%.3f' % speed,
                '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-movflags', '+faststart']
    else:
        args = ['-vf', v_filter, '-an', '-c:v', 'libx264',
                '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


# ---------------------------------------------------------------- metin / altyazi / filtre
_COLOR_NAMES = {
    'white': (255, 255, 255, 255),
    'black': (0, 0, 0, 255),
    'red': (220, 38, 38, 255),
    'yellow': (250, 204, 21, 255),
    'green': (34, 197, 94, 255),
    'blue': (59, 130, 246, 255),
    'cyan': (6, 182, 212, 255),
}


def _parse_rgba(color_val, default=(255, 255, 255, 255)):
    s = str(color_val or '').strip().lower()
    if s in _COLOR_NAMES:
        return _COLOR_NAMES[s]
    if s.startswith('#') and len(s) in (7, 9):
        try:
            r = int(s[1:3], 16)
            g = int(s[3:5], 16)
            b = int(s[5:7], 16)
            a = int(s[7:9], 16) if len(s) == 9 else 255
            return (r, g, b, a)
        except ValueError:
            pass
    return default


def _load_overlay_font(size: int):
    from PIL import ImageFont
    for candidate in (
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf',
        '/system/fonts/Roboto-Bold.ttf',
        '/system/fonts/NotoSans-Bold.ttf',
    ):
        if os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap_overlay_lines(draw, text: str, font, max_width: int) -> list:
    lines = []
    for raw_line in str(text or '').splitlines():
        words = raw_line.split()
        if not words:
            lines.append('')
            continue
        cur = words[0]
        for w in words[1:]:
            trial = cur + ' ' + w
            bbox = draw.textbbox((0, 0), trial, font=font)
            if (bbox[2] - bbox[0]) <= max_width:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines or ['']


def _render_text_overlay_image(text: str, width: int, height: int, position: str = 'bottom',
                               font_size: int = 0, color: str = '#FFFFFF', box: bool = True):
    """Turkce Unicode destekli seffaf RGBA metin katmani olusturur."""
    from PIL import Image, ImageDraw

    w = max(64, int(width))
    h = max(64, int(height))
    fsize = int(font_size) if font_size else max(18, min(96, int(min(w, h) * 0.065)))
    fsize = max(14, min(160, fsize))
    font = _load_overlay_font(fsize)

    layer = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    max_text_w = max(40, int(w * 0.86))
    lines = _wrap_overlay_lines(draw, text, font, max_text_w)

    line_boxes = [draw.textbbox((0, 0), ln or ' ', font=font) for ln in lines]
    line_heights = [max(fsize, b[3] - b[1]) for b in line_boxes]
    line_widths = [b[2] - b[0] for b in line_boxes]
    gap = max(6, int(fsize * 0.28))
    total_h = sum(line_heights) + gap * max(0, len(lines) - 1)
    max_w = max(line_widths) if line_widths else 20

    pad_x = max(14, int(fsize * 0.6))
    pad_y = max(10, int(fsize * 0.4))
    margin = max(16, int(min(w, h) * 0.05))
    pos = str(position or 'bottom').strip().lower()

    if pos in ('top', 'top-left', 'top-right'):
        top_y = margin
    elif pos == 'center':
        top_y = max(margin, (h - total_h) // 2)
    else:
        top_y = max(margin, h - total_h - margin - pad_y)

    if pos in ('top-left', 'bottom-left'):
        box_x0 = margin
    elif pos in ('top-right', 'bottom-right'):
        box_x0 = max(margin, w - max_w - pad_x * 2 - margin)
    else:
        box_x0 = max(8, (w - max_w) // 2 - pad_x)

    box_x1 = min(w - 8, box_x0 + max_w + pad_x * 2)
    box_y0 = max(8, top_y - pad_y)
    box_y1 = min(h - 8, top_y + total_h + pad_y)

    if box:
        draw.rounded_rectangle(
            [box_x0, box_y0, box_x1, box_y1],
            radius=max(8, int(fsize * 0.35)),
            fill=(9, 11, 16, 175),
        )

    fill_rgba = _parse_rgba(color, (255, 255, 255, 255))
    cur_y = top_y
    for idx, ln in enumerate(lines):
        lw = line_widths[idx]
        if pos in ('top-left', 'bottom-left'):
            tx = box_x0 + pad_x
        elif pos in ('top-right', 'bottom-right'):
            tx = max(box_x0 + pad_x, box_x1 - pad_x - lw)
        else:
            tx = max(8, (w - lw) // 2)
        draw.text((tx + 2, cur_y + 2), ln, font=font, fill=(0, 0, 0, 210))
        draw.text((tx, cur_y), ln, font=font, fill=fill_rgba)
        cur_y += line_heights[idx] + gap
    return layer


def _probe_dimensions(filepath: str):
    info = get_media_info(filepath) or {}
    for s in info.get('streams', []):
        if s.get('codec_type') == 'video' and s.get('width') and s.get('height'):
            return int(s['width']), int(s['height'])
    return 1280, 720


def _op_add_text(input_files, output_path, params):
    """Gorsel veya video uzerine Turkce Unicode destekli metin bindirir."""
    if not input_files:
        return False, "Metin eklemek icin gorsel veya video dosyasi gerekli"
    text = str(params.get('text') or params.get('caption') or '').strip()
    if not text:
        return False, "Eklenecek metin (text) bos olamaz"
    if len(text) > 500:
        text = text[:500]

    src = input_files[0]
    src_ext = Path(src).suffix.lower()
    position = str(params.get('position', 'bottom')).strip().lower()
    font_size = _as_int(params.get('font_size', 0), 0)
    color = str(params.get('color', '#FFFFFF')).strip()
    box = bool(params.get('box', True))

    if src_ext in IMAGE_EXTS:
        try:
            from PIL import Image
            with Image.open(src) as base_im:
                base = base_im.convert('RGBA')
                overlay = _render_text_overlay_image(
                    text, base.width, base.height,
                    position=position, font_size=font_size, color=color, box=box,
                )
                combined = Image.alpha_composite(base, overlay)
                out = output_path.with_suffix('.png')
                combined.save(str(out), format='PNG')
                return True, out
        except Exception as e:
            return False, "Gorsele metin eklenemedi: %s" % api_utils.safe_error_message(e)

    w, h = _probe_dimensions(src)
    overlay_path = Path(MEDIA_TEMP_DIR) / ('txt_%s.png' % uuid.uuid4().hex)
    try:
        overlay = _render_text_overlay_image(
            text, w, h, position=position, font_size=font_size, color=color, box=box,
        )
        overlay.save(str(overlay_path), format='PNG')
        start_t = params.get('start')
        end_t = params.get('end')
        enable_expr = ''
        if start_t is not None and end_t is not None:
            s_val = max(0.0, _as_float(start_t, 0.0))
            e_val = max(s_val + 0.1, _as_float(end_t, s_val + 5.0))
            enable_expr = ":enable='between(t,%.3f,%.3f)'" % (s_val, e_val)

        out = output_path.with_suffix('.mp4')
        fc = '[0:v][1:v]overlay=0:0%s,format=yuv420p[v]' % enable_expr
        args = [
            '-filter_complex', fc, '-map', '[v]', '-map', '0:a?',
            '-c:v', 'libx264', '-preset', 'veryfast', '-c:a', 'copy', '-movflags', '+faststart',
        ]
        ok, res = run_ffmpeg(args, input_files=[src, str(overlay_path)], output_file=str(out))
        return (True, out) if ok else (False, res)
    except Exception as e:
        return False, api_utils.safe_error_message(e)
    finally:
        try:
            if overlay_path.exists():
                overlay_path.unlink()
        except Exception:
            pass


def _srt_ts_to_seconds(ts_str: str) -> float:
    s = str(ts_str or '').strip().replace(',', '.')
    parts = s.split(':')
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(s)
    except ValueError:
        return 0.0


def _parse_subtitle_cues(params: dict) -> list:
    cues = []
    raw_list = params.get('subtitles')
    if isinstance(raw_list, list):
        for item in raw_list[:50]:
            if not isinstance(item, dict):
                continue
            txt = str(item.get('text') or '').strip()
            if not txt:
                continue
            st = max(0.0, _as_float(item.get('start', 0), 0.0))
            en = max(st + 0.2, _as_float(item.get('end', st + 2.5), st + 2.5))
            cues.append({'start': st, 'end': en, 'text': txt[:300]})
        return cues

    srt = str(params.get('srt') or '').strip()
    if not srt:
        return []
    blocks = re.split(r'\r?\n\s*\r?\n', srt)
    for blk in blocks[:50]:
        lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
        if not lines:
            continue
        ts_idx = 0
        if '-->' not in lines[0] and len(lines) > 1 and '-->' in lines[1]:
            ts_idx = 1
        if '-->' not in lines[ts_idx]:
            continue
        left, right = lines[ts_idx].split('-->', 1)
        st = max(0.0, _srt_ts_to_seconds(left))
        en = max(st + 0.2, _srt_ts_to_seconds(right.split()[0]))
        txt = '\n'.join(lines[ts_idx + 1:]).strip()
        if txt:
            cues.append({'start': st, 'end': en, 'text': txt[:300]})
    return cues


def _op_burn_subtitles(input_files, output_path, params):
    """Videoya zamanli altyazi (SRT veya JSON cue listesi) gomer."""
    if not input_files:
        return False, "Altyazi icin video dosyasi gerekli"
    cues = _parse_subtitle_cues(params or {})
    if not cues:
        return False, "Gecerli altyazi (srt veya subtitles) bulunamadi"

    src = input_files[0]
    w, h = _probe_dimensions(src)
    font_size = _as_int(params.get('font_size', 0), 0)
    color = str(params.get('color', '#FFFFFF')).strip()

    tmp_pngs = []
    try:
        in_list = [src]
        fc_parts = []
        prev = '[0:v]'
        for idx, cue in enumerate(cues, start=1):
            p = Path(MEDIA_TEMP_DIR) / ('sub_%s_%d.png' % (uuid.uuid4().hex[:8], idx))
            im = _render_text_overlay_image(
                cue['text'], w, h, position='bottom', font_size=font_size, color=color, box=True,
            )
            im.save(str(p), format='PNG')
            tmp_pngs.append(p)
            in_list.append(str(p))
            out_tag = '[v%d]' % idx if idx < len(cues) else '[vout_raw]'
            fc_parts.append(
                "%s[%d:v]overlay=0:0:enable='between(t,%.3f,%.3f)'%s"
                % (prev, idx, cue['start'], cue['end'], out_tag)
            )
            prev = out_tag

        fc = ';'.join(fc_parts) + ';%sformat=yuv420p[vout]' % prev
        out = output_path.with_suffix('.mp4')
        args = [
            '-filter_complex', fc, '-map', '[vout]', '-map', '0:a?',
            '-c:v', 'libx264', '-preset', 'veryfast', '-c:a', 'copy', '-movflags', '+faststart',
        ]
        ok, res = run_ffmpeg(args, input_files=in_list, output_file=str(out))
        return (True, out) if ok else (False, res)
    except Exception as e:
        return False, api_utils.safe_error_message(e)
    finally:
        for p in tmp_pngs:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass


_FILTER_PRESETS = {
    'grayscale': 'hue=s=0',
    'bw': 'hue=s=0',
    'sepia': 'colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131',
    'blur': 'boxblur=4:1',
    'sharpen': 'unsharp=5:5:1.2:5:5:0.0',
    'vignette': 'vignette=PI/4',
    'invert': 'negate',
    'vintage': 'curves=vintage,vignette=PI/5',
    'bright': 'eq=brightness=0.14:contrast=1.05',
    'dark': 'eq=brightness=-0.16:contrast=1.08',
    'contrast': 'eq=contrast=1.35:saturation=1.12',
    'warm': 'colortemperature=temperature=4500',
    'cool': 'colortemperature=temperature=8500',
    'hflip': 'hflip',
    'vflip': 'vflip',
    'rotate_90': 'transpose=1',
    'rotate_180': 'transpose=1,transpose=1',
    'rotate_270': 'transpose=2',
}


def _op_apply_filter(input_files, output_path, params):
    """Gorsel veya videoya renk/efekt/yonlendirme filtresi uygular."""
    if not input_files:
        return False, "Filtre uygulamak icin dosya gerekli"
    src = input_files[0]
    src_ext = Path(src).suffix.lower()
    name = str(params.get('filter', 'grayscale')).strip().lower()

    if name == 'custom':
        br = max(-0.5, min(_as_float(params.get('brightness', 0.0), 0.0), 0.5))
        ct = max(0.3, min(_as_float(params.get('contrast', 1.0), 1.0), 2.5))
        sat = max(0.0, min(_as_float(params.get('saturation', 1.0), 1.0), 3.0))
        vf = 'eq=brightness=%.2f:contrast=%.2f:saturation=%.2f' % (br, ct, sat)
    elif name in _FILTER_PRESETS:
        vf = _FILTER_PRESETS[name]
    else:
        return False, "Desteklenmeyen filtre: %s" % name

    if src_ext in IMAGE_EXTS:
        out = output_path.with_suffix('.png')
        args = ['-vf', vf, '-frames:v', '1']
        ok, res = run_ffmpeg(args, input_files=[src], output_file=str(out))
        return (True, out) if ok else (False, res)

    out = output_path.with_suffix('.mp4')
    args = [
        '-vf', vf + ',format=yuv420p',
        '-c:v', 'libx264', '-preset', 'veryfast', '-c:a', 'copy', '-movflags', '+faststart',
    ]
    ok, res = run_ffmpeg(args, input_files=[src], output_file=str(out))
    return (True, out) if ok else (False, res)


# ---------------------------------------------------------------- genel
def _op_convert(input_files, output_path, params):
    if not input_files:
        return False, "Donusturme icin dosya gerekli"
    fmt = str(params.get('format', 'mp4')).lower()
    ext = CONVERT_EXT_MAP.get(fmt, '.mp4')
    out = output_path.with_suffix(ext)
    if fmt in ('mp3', 'wav', 'aac', 'm4a', 'ogg', 'flac'):
        codec_map = {
            'mp3': 'libmp3lame', 'wav': 'pcm_s16le', 'aac': 'aac',
            'm4a': 'aac', 'ogg': 'libvorbis', 'flac': 'flac',
        }
        args = ['-vn', '-acodec', codec_map[fmt]]
    elif fmt in ('png', 'jpg', 'jpeg', 'webp'):
        args = ['-frames:v', '1']
    elif fmt == 'gif':
        args = ['-vf', 'fps=10,scale=480:-1:flags=lanczos', '-loop', '0']
    elif fmt == 'webm':
        args = ['-c:v', 'libvpx-vp9', '-b:v', '1M', '-c:a', 'libopus']
    elif fmt == '3gp':
        args = ['-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '64k', '-s', '640x480']
    else:
        args = ['-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-movflags', '+faststart']
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


def _op_resize(input_files, output_path, params):
    if not input_files:
        return False, "Yeniden boyutlandirma icin dosya gerekli"
    src_ext = Path(input_files[0]).suffix.lower()
    if src_ext in IMAGE_EXTS:
        w = max(16, min(_as_int(params.get('width', 1280), 1280), 3840))
        h = max(16, min(_as_int(params.get('height', 720), 720), 3840))
        out = output_path.with_suffix('.jpg')
        args = ['-vf', 'scale=%d:%d:force_original_aspect_ratio=decrease,'
                       'pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1' % (w, h, w, h),
                '-frames:v', '1']
    else:
        w = _clamp_even(params.get('width', 1280), 1280, lo=16, hi=3840)
        h = _clamp_even(params.get('height', 720), 720, lo=16, hi=3840)
        out = output_path.with_suffix('.mp4')
        args = ['-vf', 'scale=%d:%d:force_original_aspect_ratio=decrease,'
                       'pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p' % (w, h, w, h),
                '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                '-c:a', 'copy', '-movflags', '+faststart']
    ok, res = run_ffmpeg(args, input_files=input_files, output_file=str(out))
    return (True, out) if ok else (False, res)


# ===================================================================== yardimcilar
def new_output_path(suffix=None, operation=None, params=None):
    """Yeni bir cikti dosyasi yolu uretir."""
    if not suffix:
        suffix = operation_output_ext(operation, params)
    return Path(MEDIA_TEMP_DIR) / ("%s%s" % (uuid.uuid4().hex, suffix))


def operation_output_ext(operation, params=None):
    """Islemin uretecegi dosya uzantisini dondurur."""
    operation = str(operation or '').strip()
    if operation == 'convert':
        fmt = str((params or {}).get('format', 'mp4')).lower()
        return CONVERT_EXT_MAP.get(fmt, '.mp4')
    if operation == 'extract_audio':
        fmt = str((params or {}).get('format', 'mp3')).lower()
        return AUDIO_EXT_MAP.get(fmt, '.mp3')
    if operation == 'compress_audio':
        fmt = str((params or {}).get('format', 'mp3')).lower()
        return '.m4a' if fmt == 'm4a' else '.mp3'
    return OPERATION_OUTPUT_EXT.get(operation, '.mp4')


def result_descriptor(operation, path, extra=None):
    """Standart media sonuc tanimlayicisi uretir."""
    p = Path(path)
    try:
        size = p.stat().st_size
    except Exception:
        size = 0
    descriptor = {
        'success': True,
        'operation': operation,
        'label': OPERATION_LABELS.get(operation, operation),
        'output_file': p.name,
        'download_url': '/api/media/download/%s' % p.name,
        'size': size,
        'mime_type': guess_mime(p) or 'application/octet-stream',
    }
    if extra:
        descriptor.update(extra)
    return descriptor
