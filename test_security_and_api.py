#!/usr/bin/env python3
"""
ASLAN Technologies - Tam Guvenlik, Auth/DB, Logo, Cerez, FFmpeg ve Coklu AI Test Suiti.
"""
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

WORK = Path(tempfile.mkdtemp(prefix="aslan_sec_"))
os.environ["ASLAN_TMP_DIR"] = str(WORK / "tmp")
os.environ["ASLAN_DATA_DIR"] = str(WORK / "data")
os.environ["ASLAN_DISABLE_BLOB_DB"] = "1"
os.environ["QUOTA_SIGNING_SECRET"] = "test-signing-secret-12345"
os.environ["AUTH_SECRET"] = "test-auth-secret-67890"

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  {PASS if ok else FAIL}  {name}" + (f"  → {detail}" if detail else ""))
    results.append((name, ok, detail))
    return ok


def section(title):
    print(f"\n\033[1;36m{'=' * 60}\n{title}\n{'=' * 60}\033[0m")


def make_image(path, color="red", size="320x240"):
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"color=c={color}:s={size}", "-frames:v", "1", path
    ], check=True)


def make_audio(path, seconds=2):
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={seconds}", path
    ], check=True)


def make_video(path, seconds=2):
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"color=c=blue:s=320x240:d={seconds}:r=25",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path
    ], check=True)


class _CaseHeaders(dict):
    def get(self, key, default=None):
        kl = str(key).lower()
        for k, v in self.items():
            if str(k).lower() == kl:
                return v
        return default


class FakeHandler:
    """api/*.py BaseHTTPRequestHandler siniflarini soketsiz test eder."""

    def __init__(self, handler_cls, method="GET", body=b"", headers=None, path="/"):
        self.status = None
        self.resp_headers = []
        self.wfile = io.BytesIO()
        self.rfile = io.BytesIO(body)
        self.path = path
        hdrs = dict(headers or {})
        if body and "Content-Length" not in hdrs:
            hdrs["Content-Length"] = str(len(body))
        self.headers = _CaseHeaders(hdrs)
        self._cls = handler_cls
        self._method = method

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.resp_headers.append((k, v))

    def end_headers(self):
        pass

    def run(self):
        inst = object.__new__(self._cls)
        inst.rfile = self.rfile
        inst.wfile = self.wfile
        inst.headers = self.headers
        inst.path = self.path
        inst.send_response = self.send_response
        inst.send_header = self.send_header
        inst.end_headers = self.end_headers
        getattr(inst, f"do_{self._method}")()
        return self

    def header_map(self):
        out = {}
        for k, v in self.resp_headers:
            out.setdefault(k.lower(), []).append(v)
        return out

    def json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def main():
    import server
    from core import ai_engine, api_utils, auth, config as cfg, database, media_engine

    app = server.app
    client = app.test_client()

    # ------------------------------------------------------------------
    section("1. LOGO VARLIKLARI, STATIK IZOLASYON VE CEREZ BAR YAPISI")
    for secret_path in (
        "/.env.local", "/.git/config", "/server.py", "/core/config.py",
        "/requirements.txt", "/src/index.html", "/scripts/build_frontend.mjs",
        "/assets/app.min.js.map",
    ):
        r = client.get(secret_path)
        check(f"Blocked static {secret_path} → 404", r.status_code == 404, str(r.status_code))

    for pub_path in (
        "/", "/manifest.json", "/robots.txt", "/sw.js",
        "/assets/app.min.css", "/assets/app.min.js",
        "/assets/logo-mark.png", "/assets/favicon.png",
        "/assets/icon-192.png", "/assets/icon-512.png",
        "/assets/icon-maskable-512.png", "/assets/apple-touch-icon.png",
        "/assets/logo-banner.png",
    ):
        r = client.get(pub_path)
        check(f"Public asset {pub_path} → 200", r.status_code == 200, str(r.status_code))
        if pub_path.endswith(".png"):
            check(f"  {pub_path} is image/png", "image/png" in (r.headers.get("Content-Type") or ""))

    html_txt = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    js_txt = (ROOT / "public" / "assets" / "app.min.js").read_text(encoding="utf-8")
    css_txt = (ROOT / "public" / "assets" / "app.min.css").read_text(encoding="utf-8")
    sw_txt = (ROOT / "public" / "sw.js").read_text(encoding="utf-8")

    banner_block = html_txt.split('id="cookie-banner"')[1].split('id="cookie-modal"')[0]
    check("Cookie banner has 'Çerezleri Kabul Et'", "Çerezleri Kabul Et" in banner_block)
    check("Cookie banner has 'Çerezleri Yönet'", "Çerezleri Yönet" in banner_block)
    check("Cookie banner does NOT show reject button directly", "Çerezleri Reddet" not in banner_block)
    modal_block = html_txt.split('id="cookie-modal"')[1].split('id="auth-modal"')[0]
    check("Cookie detail modal has 'Çerezleri Reddet' at bottom", "Çerezleri Reddet" in modal_block)
    check("Cookie reset button & function completely removed",
          "resetCookieConsent" not in html_txt and "resetCookieConsent" not in js_txt
          and "Çerez Tercihini Sıfırla" not in html_txt)
    check("Auth lock overlays present for TTS and AI",
          'id="tts-auth-lock"' in html_txt and 'id="ai-auth-lock"' in html_txt)
    check("Strict CSP HTML: zero inline scripts, zero inline event handlers, zero inline styles",
          "<script>" not in html_txt
          and not re.search(r'\bon[a-z]+\s*=', html_txt, re.IGNORECASE)
          and not re.search(r'\bstyle\s*=', html_txt, re.IGNORECASE))
    check("window.name auth token storage removed", "AUTH_WIN_PREFIX" not in js_txt)

    # ------------------------------------------------------------------
    section("2. HTTP GUVENLIK BASLIKLARI, STRICT CSP, CSRF & KONTROLLU CORS")
    r_home = client.get("/")
    csp = r_home.headers.get("Content-Security-Policy", "")
    check("HTML has Strict Content-Security-Policy (no unsafe-inline)",
          "script-src 'self'" in csp and "style-src 'self'" in csp
          and "'unsafe-inline'" not in csp and "worker-src 'self'" in csp
          and "manifest-src 'self'" in csp, csp)
    check("HTML has X-Frame-Options: DENY", r_home.headers.get("X-Frame-Options") == "DENY")
    check("HTML has X-Content-Type-Options: nosniff", r_home.headers.get("X-Content-Type-Options") == "nosniff")
    check("HTML has Referrer-Policy", "Referrer-Policy" in r_home.headers)
    check("HTML has Permissions-Policy", "Permissions-Policy" in r_home.headers)
    check("HTML has COOP, CORP, X-Permitted-Cross-Domain-Policies, X-Download-Options",
          r_home.headers.get("Cross-Origin-Opener-Policy") == "same-origin"
          and r_home.headers.get("Cross-Origin-Resource-Policy") == "same-origin"
          and r_home.headers.get("X-Permitted-Cross-Domain-Policies") == "none"
          and r_home.headers.get("X-Download-Options") == "noopen")

    r_api = client.get("/api/health", headers={"Origin": "https://aslansite.vercel.app"})
    check("GET /api/health → 200", r_api.status_code == 200 and r_api.get_json()["capabilities"]["auth"])
    check("API has Cache-Control: no-store", "no-store" in (r_api.headers.get("Cache-Control") or ""))
    check("Allowed Origin echoed in ACAO",
          r_api.headers.get("Access-Control-Allow-Origin") == "https://aslansite.vercel.app")

    r_evil_post = client.post("/api/quota", json={}, headers={"Origin": "https://evil.example"})
    check("Disallowed Origin POST → 403", r_evil_post.status_code == 403, str(r_evil_post.status_code))

    r_evil_vercel = client.post("/api/quota", json={}, headers={"Origin": "https://aslansite-evil.vercel.app"})
    check("Wildcard aslansite-* Vercel Origin POST → 403", r_evil_vercel.status_code == 403, str(r_evil_vercel.status_code))

    r_cross_site = client.post("/api/quota", json={}, headers={"Sec-Fetch-Site": "cross-site"})
    check("Sec-Fetch-Site: cross-site POST → 403", r_cross_site.status_code == 403, str(r_cross_site.status_code))

    r_bad_referer = client.post("/api/quota", json={}, headers={"Referer": "https://evil.example/form.html"})
    check("Foreign Referer POST → 403", r_bad_referer.status_code == 403, str(r_bad_referer.status_code))

    # ------------------------------------------------------------------
    section("3. ZORUNLU ERISIM KONTROLU, KAYIT KURALLARI VE CEREZSIZ OTURUM")
    check("Unauthenticated /api/ai → 401", client.post("/api/ai", json={"message": "hi"}).status_code == 401)
    check("Unauthenticated /api/tts → 401", client.post("/api/tts", json={"text": "merhaba"}).status_code == 401)
    check("Unauthenticated /api/image → 401", client.post("/api/image", json={"prompt": "kedi"}).status_code == 401)
    check("Unauthenticated /api/auth/me → 401", client.get("/api/auth/me").status_code == 401)

    bad_regs = [
        ("ab", "Aslan.Strong12!", "Aslan.Strong12!", "username < 3 chars"),
        ("aslan1", "Aslan.Strong12!", "Aslan.Other99!", "password mismatch"),
        ("aslan1", "Aslan.12a", "Aslan.12a", "password < 12 chars"),
        ("aslan1", "aslan.lowercase12", "aslan.lowercase12", "missing uppercase"),
        ("aslan1", "ASLAN.UPPERCASE12", "ASLAN.UPPERCASE12", "missing lowercase"),
        ("aslan1", "Aslan.NoDigitsHere!", "Aslan.NoDigitsHere!", "missing digit"),
        ("aslan1", "AslanNoSpecial123", "AslanNoSpecial123", "missing special symbol"),
    ]
    for u, p1, p2, label in bad_regs:
        rr = client.post("/api/auth/register", json={"username": u, "password": p1, "password_confirm": p2})
        check(f"Register rejects ({label}) → 400", rr.status_code == 400, str(rr.status_code))

    good_pw = "Aslan.Tech2026!"
    r_reg = client.post("/api/auth/register", json={
        "username": "AslanMaster",
        "password": good_pw,
        "password_confirm": good_pw,
    })
    reg_data = r_reg.get_json() or {}
    check("Valid registration → 201 + Bearer token",
          r_reg.status_code == 201 and str(reg_data.get("token", "")).startswith("aslan1."))

    r_dup = client.post("/api/auth/register", json={
        "username": "aslanmaster",
        "password": good_pw,
        "password_confirm": good_pw,
    })
    check("Duplicate username (case-insensitive) → 409", r_dup.status_code == 409, str(r_dup.status_code))

    # Turkce karakter iceren kullanici adi ve sifre ile kayit + giris
    tr_pw = "Aslan.Şifre2026!"
    r_tr = client.post("/api/auth/register", json={
        "username": "Müslüm_Ömer",
        "password": tr_pw,
        "password_confirm": tr_pw,
    })
    check("Turkish username & password chars register → 201",
          r_tr.status_code == 201 and bool((r_tr.get_json() or {}).get("token")), str(r_tr.status_code))
    r_tr_login = client.post("/api/auth/login", json={"username": "müslüm_ömer", "password": tr_pw})
    check("Turkish username login (casefold) → 200", r_tr_login.status_code == 200, str(r_tr_login.status_code))

    # Veritabaninda sifre duz metin olmamali
    stored_u = database.get_user_by_username("AslanMaster")
    check("Password stored as PBKDF2-SHA256 hash",
          stored_u and stored_u["password_hash"].startswith("pbkdf2_sha256$") and good_pw not in stored_u["password_hash"])

    # Encrypt-then-MAC Blob serialization testi
    enc_bytes = database.encrypt_record(stored_u)
    dec_obj = database.decrypt_record(enc_bytes)
    check("Encrypted DB record round-trip OK",
          dec_obj and dec_obj["username"] == "AslanMaster")
    check("Tampered encrypted DB record rejected",
          database.decrypt_record(enc_bytes[:-1] + bytes([enc_bytes[-1] ^ 0xFF])) is None)

    token = reg_data["token"]
    # Cerezsiz yalniz Authorization header ile erisim
    r_me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    check("Cookieless /api/auth/me with Bearer → 200", r_me.status_code == 200 and r_me.get_json()["user"]["username"] == "AslanMaster")

    # Hesaba bagli kalici kota testi: 1 AI mesaji tuket, farkli tarayicidan/cihazdan ayni hesaba gir
    with mock.patch.object(ai_engine, "generate_reply", return_value=("Merhaba Aslan!", None)):
        r_ai1 = client.post("/api/ai", json={"message": "Selam"},
                            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "browser-one"})
    check("Authenticated AI call consumes 1 from DB quota (4/5 left)",
          r_ai1.status_code == 200 and r_ai1.get_json()["quota"]["ai"]["remaining"] == 4)

    r_login2 = client.post("/api/auth/login", json={"username": "AslanMaster", "password": good_pw},
                           headers={"X-Device-Id": "completely-different-browser"})
    check("Login from another browser sees persisted remaining=4",
          r_login2.status_code == 200 and r_login2.get_json()["quota"]["ai"]["remaining"] == 4)

    # Logout testi
    token2 = r_login2.get_json()["token"]
    r_out = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token2}"})
    r_after_out = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token2}"})
    check("Logout revokes token → 401", r_out.status_code == 200 and r_after_out.status_code == 401)

    # ------------------------------------------------------------------
    section("4. SSRF KORUMASI (/api/media/social)")
    for bu in (
        "http://127.0.0.1/youtube.com",
        "http://169.254.169.254/latest/meta-data/",
        "https://evil.com/youtube.com/watch?v=1",
        "https://fakeyoutube.com/watch?v=1",
        "file:///etc/passwd",
    ):
        r = client.post("/api/media/social", json={"url": bu})
        check(f"SSRF blocked → 400: {bu[:35]}", r.status_code == 400, str(r.status_code))

    # ------------------------------------------------------------------
    section("5. FFMPEG TAM KAPASITE (19 ISLEM, RESIZE VIDEO, XFADE, NEW OPS)")
    imgs = []
    for idx, color in enumerate(("red", "green", "blue")):
        p = WORK / f"img_{idx}.png"
        make_image(str(p), color=color)
        imgs.append(str(p))
    aud = WORK / "bg.mp3"
    make_audio(str(aud), seconds=6)
    vid = WORK / "vid.mp4"
    make_video(str(vid), seconds=2)

    ok_res, p_res = media_engine.execute_media_operation("resize", [str(vid)], WORK / "r.mp4", {"width": 640, "height": 480})
    check("resize (video 640x480) succeeds", ok_res and Path(p_res).suffix == ".mp4" and Path(p_res).stat().st_size > 1000)

    ok_ca, p_ca = media_engine.execute_media_operation("compress_audio", [str(aud)], WORK / "ca.mp3", {"bitrate": "64k", "format": "mp3"})
    check("compress_audio succeeds", ok_ca and Path(p_ca).stat().st_size > 500)

    ok_mv, p_mv = media_engine.execute_media_operation("mute_video", [str(vid)], WORK / "mv.mp4", {})
    info_mv = media_engine.get_media_info(str(p_mv)) if ok_mv else None
    types_mv = [s.get("codec_type") for s in (info_mv or {}).get("streams", [])]
    check("mute_video removes audio stream", ok_mv and "video" in types_mv and "audio" not in types_mv, str(types_mv))

    ok_sp, p_sp = media_engine.execute_media_operation("change_speed", [str(vid)], WORK / "sp.mp4", {"speed": 1.5})
    check("change_speed (video 1.5x) succeeds", ok_sp and Path(p_sp).stat().st_size > 1000)

    ok_st, p_st = media_engine.execute_media_operation(
        "stitch_media", imgs, WORK / "st.mp4",
        {"transition": "fade", "per_image_duration": 2.0, "transition_duration": 0.5},
    )
    check("stitch_media 3-photo fade succeeds", ok_st and Path(p_st).stat().st_size > 1000)

    # ------------------------------------------------------------------
    section("6. COKLU AI YEDEK ZINCIRI & AI-KONTROLLU TTS")
    # 6.1 Ucretsiz/anahtarsiz uc calisinca Gemini'ye hic dokunmamali
    called_urls = []

    def fake_free_ok(url, payload, timeout=90, headers=None):
        called_urls.append(url)
        return {"choices": [{"message": {"content": "Ucretsiz AI yaniti!"}}]}

    with mock.patch.object(ai_engine, "_post_json", side_effect=fake_free_ok):
        r_txt, e_txt = ai_engine.generate_reply("Merhaba", api_key="AIzaDummyKeyShouldNotBeCalledFirst")
    check("Free endpoint tried FIRST (Gemini not called when free works)",
          r_txt == "Ucretsiz AI yaniti!" and any("pollinations.ai" in u for u in called_urls)
          and not any("generativelanguage.googleapis.com" in u for u in called_urls), str(called_urls))

    # 6.2 Tum ucretsiz uclar hata verirse en son Gemini devreye girmeli (x-goog-api-key basligi ile)
    fallback_calls = []

    def fake_all_free_fail(url, payload, timeout=90, headers=None):
        fallback_calls.append((url, headers or {}))
        if "generativelanguage.googleapis.com" in url:
            return {"candidates": [{"content": {"parts": [{"text": "Gemini son durak yaniti!"}]}}]}
        raise RuntimeError("Free endpoint unavailable")

    with mock.patch.object(ai_engine, "_post_json", side_effect=fake_all_free_fail), \
         mock.patch.object(ai_engine, "_post_raw_text", side_effect=RuntimeError("Free raw fail")), \
         mock.patch.object(ai_engine, "_get_raw_text", side_effect=RuntimeError("Free get fail")):
        r_gem, e_gem = ai_engine.generate_reply("Merhaba", api_key="AIzaTestFallbackKey999")
    gem_headers = [h for u, h in fallback_calls if "generativelanguage.googleapis.com" in u]
    check("Gemini acts as LAST RESORT when all free endpoints fail",
          r_gem == "Gemini son durak yaniti!" and gem_headers and gem_headers[0].get("x-goog-api-key") == "AIzaTestFallbackKey999")

    # 6.3 TTS kontrol eylemi (tts_control)
    ctrl = ai_engine.run_action({
        "action": "tts_control",
        "params": {"command": "stop", "voice": "tr-TR-EmelNeural", "rate": "+20%", "pitch": "-4Hz"},
    })
    check("AI tts_control action parsed & normalized",
          ctrl.get("success") and ctrl.get("operation") == "tts_control" and ctrl.get("command") == "stop" and ctrl.get("rate") == "+20%")

    # ------------------------------------------------------------------
    section("7. VERCEL TEK ROUTER HANDLER (api/index.py -> core/handlers/*)")
    from api import index as api_index

    h_res = FakeHandler(api_index.handler, "GET", path="/api/index.py?__route=/api/health").run()
    check("api/index.py /api/health → 200", h_res.status == 200 and h_res.json().get("capabilities", {}).get("free_ai"))

    # Vercel auth handler: register -> me -> AI -> TTS
    v_reg = FakeHandler(
        api_index.handler, "POST", path="/api/index.py?__route=/api/auth/register",
        body=json.dumps({"username": "VercelUser", "password": good_pw, "password_confirm": good_pw}).encode("utf-8"),
    ).run()
    check("api/index.py /api/auth/register → 201", v_reg.status == 201 and v_reg.json().get("token"))
    v_tok = v_reg.json()["token"]

    v_me = FakeHandler(
        api_index.handler, "GET", path="/api/index.py?__route=/api/auth/me",
        headers={"Authorization": f"Bearer {v_tok}"},
    ).run()
    check("api/index.py /api/auth/me → 200", v_me.status == 200 and v_me.json().get("authenticated"))

    v_ai_unauth = FakeHandler(api_index.handler, "POST", path="/api/index.py?__route=/api/ai", body=b'{"message":"hi"}').run()
    v_tts_unauth = FakeHandler(api_index.handler, "POST", path="/api/index.py?__route=/api/tts", body=b'{"text":"hi"}').run()
    check("api/index.py /api/ai & /api/tts unauthenticated → 401", v_ai_unauth.status == 401 and v_tts_unauth.status == 401)

    with mock.patch.object(ai_engine, "generate_reply", return_value=("Vercel AI yaniti", None)):
        v_ai_ok = FakeHandler(
            api_index.handler, "POST", path="/api/index.py?__route=/api/ai",
            body=json.dumps({"message": "Merhaba"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {v_tok}"},
        ).run()
    check("api/index.py /api/ai authenticated → 200 + DB quota=4",
          v_ai_ok.status == 200 and v_ai_ok.json()["quota"]["ai"]["remaining"] == 4)

    v_q = FakeHandler(api_index.handler, "GET", path="/api/index.py?__route=/api/quota", headers={"Authorization": f"Bearer {v_tok}"}).run()
    check("api/index.py /api/quota returns account-bound quota=4",
          v_q.status == 200 and v_q.json().get("account_bound") and v_q.json()["ai"]["remaining"] == 4)

    # api/media.py crop + cleanup
    before_files = set(os.listdir(cfg.MEDIA_TEMP_DIR))
    img_b64 = base64.b64encode(Path(imgs[0]).read_bytes()).decode("ascii")
    m_res = FakeHandler(api_index.handler, "POST", path="/api/index.py?__route=/api/media/process", body=json.dumps({
        "operation": "crop_image",
        "files": [{"name": "a.png", "mime_type": "image/png", "data": img_b64}],
        "params": {"x": 0, "y": 0, "width": 100, "height": 80},
    }).encode("utf-8")).run()
    after_files = set(os.listdir(cfg.MEDIA_TEMP_DIR))
    check("api/index.py /api/media/process crop_image → 200 & /tmp clean", m_res.status == 200 and before_files == after_files)

    v_soc = FakeHandler(api_index.handler, "POST", path="/api/index.py?__route=/api/media/social", body=b'{"url":"http://127.0.0.1/?youtube.com"}').run()
    check("api/index.py /api/media/social SSRF → 400", v_soc.status == 400)

    v_con = FakeHandler(api_index.handler, "POST", path="/api/index.py?__route=/api/cookie-consent", body=json.dumps({
        "consent": "custom", "preferences": {"functional": True, "analytics": False}
    }).encode("utf-8")).run()
    check("api/index.py /api/cookie-consent custom prefs → 200", v_con.status == 200 and v_con.json().get("consent") == "custom")

    with mock.patch.object(ai_engine, "generate_image", return_value=(False, {"quota_exceeded": False, "error": "err"})):
        v_img_err = FakeHandler(
            api_index.handler, "POST", path="/api/index.py?__route=/api/image",
            body=json.dumps({"prompt": "kedi"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {v_tok}"},
        ).run()
    check("api/index.py /api/image upstream error → 502", v_img_err.status == 502)

    v_404 = FakeHandler(api_index.handler, "GET", path="/api/index.py?__route=/api/nope").run()
    check("api/index.py unknown route → 404", v_404.status == 404)

    # ------------------------------------------------------------------
    section("8. SOZDIZIMI KONTROLLERI (PYTHON + JS + VERCEL.JSON)")
    py_files = (
        [str(ROOT / "server.py")]
        + [str(p) for p in (ROOT / "core").glob("*.py")]
        + [str(p) for p in (ROOT / "core" / "handlers").glob("*.py")]
        + [str(p) for p in (ROOT / "api").glob("*.py")]
        + [str(p) for p in (ROOT / "worker").glob("*.py")]
    )
    cp = subprocess.run(["python3", "-m", "py_compile", *py_files], capture_output=True, text=True)
    check("All Python files py_compile OK", cp.returncode == 0, cp.stderr[:200])

    js_targets = sorted([str(p) for p in (ROOT / "src" / "js").glob("*.js")]) + [
        str(ROOT / "public" / "assets" / "app.min.js"),
        str(ROOT / "public" / "sw.js"),
        str(ROOT / "api" / "wa_bridge.mjs"),
    ]
    js_ok = True
    js_err = ""
    for jt in js_targets:
        nc = subprocess.run(["node", "--check", jt], capture_output=True, text=True)
        if nc.returncode != 0:
            js_ok = False
            js_err = f"{jt}: {nc.stderr[:180]}"
            break
    check("All modular src/js/*.js, app.min.js, sw.js & api/wa_bridge.mjs syntax valid", js_ok, js_err)

    vj_raw = (ROOT / "vercel.json").read_text(encoding="utf-8")
    vj = json.loads(vj_raw)
    rw_sources = [r.get("source") for r in vj.get("rewrites", [])]
    wa_rw_ok = (
        "/api/wa_bridge" in rw_sources
        and "/api/wa_bridge/:path*" in rw_sources
        and "/api/:path*" in rw_sources
        and rw_sources.index("/api/wa_bridge/:path*") < rw_sources.index("/api/:path*")
        and rw_sources.index("/api/wa_bridge") < rw_sources.index("/api/:path*")
    )
    fn_cfg = vj.get("functions", {})
    check("vercel.json rewrites /api/wa_bridge & /api/:path*, excludes node_modules, single api/*.py",
          any(r.get("source") == "/api/:path*" and str(r.get("destination", "")).startswith("/api/index.py")
              for r in vj.get("rewrites", []))
          and wa_rw_ok
          and "api/wa_bridge.mjs" in fn_cfg
          and "node_modules/**" in str(fn_cfg.get("api/index.py", {}).get("excludeFiles", ""))
          and len(list((ROOT / "api").glob("*.py"))) == 1)
    check("vercel.json enforces strict CSP (no unsafe-inline) & /sw.js headers",
          "'unsafe-inline'" not in vj_raw
          and any(h.get("source") == "/sw.js" for h in vj.get("headers", [])))

    # ------------------------------------------------------------------
    section("9. TEK SEFERLIK CEREZ KILIDI, DONANIM/PIL, KORUMA, INPUT DOGRULAMA & PWA")
    lock_hdrs = {"X-Device-Id": "lock-test-device-1"}
    r_lock_acc = client.post("/api/cookie-consent", json={"consent": "accepted"}, headers=lock_hdrs)
    check("Cookie consent accepted → 200 + locked=True",
          r_lock_acc.status_code == 200 and r_lock_acc.get_json().get("locked") is True)
    r_lock_rej = client.post("/api/cookie-consent", json={"consent": "rejected"}, headers=lock_hdrs)
    check("Accepted cookie cannot be reverted to rejected → 409",
          r_lock_rej.status_code == 409 and r_lock_rej.get_json().get("locked") is True,
          str(r_lock_rej.status_code))
    r_lock_cust = client.post("/api/cookie-consent", json={"consent": "custom"}, headers=lock_hdrs)
    check("Accepted cookie cannot be reverted to custom → 409", r_lock_cust.status_code == 409)
    r_lock_get = client.get("/api/cookie-consent", headers=lock_hdrs)
    check("GET consent stays accepted & locked=True",
          r_lock_get.status_code == 200 and r_lock_get.get_json().get("consent") == "accepted"
          and r_lock_get.get_json().get("locked") is True)

    # Vercel handler consent lock check
    v_con_lock = FakeHandler(
        api_index.handler, "POST", path="/api/index.py?__route=/api/cookie-consent",
        body=json.dumps({"consent": "accepted"}).encode("utf-8"),
        headers={"X-Device-Id": "vercel-lock-dev"},
    ).run()
    v_con_rev = FakeHandler(
        api_index.handler, "POST", path="/api/index.py?__route=/api/cookie-consent",
        body=json.dumps({"consent": "rejected"}).encode("utf-8"),
        headers={"X-Device-Id": "vercel-lock-dev"},
    ).run()
    check("Vercel consent handler blocks revert after accept → 409",
          v_con_lock.status == 200 and v_con_rev.status == 409)

    # Input validation / DoS guards
    check("Media process invalid params type → 400",
          client.post("/api/media/process", json={"operation": "crop_image", "files": ["a"], "params": ["bad"]}).status_code == 400)
    check("Media process invalid files type → 400",
          client.post("/api/media/process", json={"operation": "crop_image", "files": "bad", "params": {}}).status_code == 400)
    check("Media process >12 files → 400",
          client.post("/api/media/process", json={"operation": "crop_image", "files": ["f"] * 13, "params": {}}).status_code == 400)
    check("Media cleanup invalid max_age_hours → 400",
          client.post("/api/media/cleanup", json={"max_age_hours": "abc"}).status_code == 400)

    r_reg_inp = client.post("/api/auth/register", json={
        "username": "InputGuardUser", "password": good_pw, "password_confirm": good_pw
    })
    inp_tok = r_reg_inp.get_json()["token"]
    check("AI invalid files list → 400",
          client.post("/api/ai", json={"message": "hi", "files": "bad"},
                      headers={"Authorization": f"Bearer {inp_tok}"}).status_code == 400)

    # PWA checks
    r_man = client.get("/manifest.json")
    manifest = json.loads(r_man.get_data(as_text=True))
    sizes_purposes = [(ic.get("sizes"), ic.get("purpose", "")) for ic in manifest.get("icons", [])]
    check("PWA manifest standalone + 192/512/maskable icons",
          manifest.get("display") == "standalone"
          and any(s == "192x192" for s, _ in sizes_purposes)
          and any(s == "512x512" and "maskable" in p for s, p in sizes_purposes))
    r_sw = client.get("/sw.js")
    check("Service Worker served with no-store & Service-Worker-Allowed: /",
          r_sw.status_code == 200 and "no-store" in (r_sw.headers.get("Cache-Control") or "")
          and r_sw.headers.get("Service-Worker-Allowed") == "/" and "aslan-pwa-" in sw_txt)
    check("No .map files in public/",
          len(list((ROOT / "public").rglob("*.map"))) == 0)

    # Pre-consent screen/GPU, silent battery, source guard & responsive CSS checks
    check("Bundle includes pre-consent screen, WebGL GPU, Battery API & source protection",
          "WEBGL_debug_renderer_info" in js_txt and "window.screen" in js_txt
          and "getBattery" in js_txt and "contextmenu" in js_txt and "F12" in js_txt)
    check("Responsive & eco CSS rules built into app.min.css",
          "max-width:640px" in css_txt and 'data-power-mode="eco"' in css_txt)

    # ------------------------------------------------------------------
    section("10. AYRI FFMPEG SEKMESI YOK, AYLIK 2 GORSEL, FREE IMAGE FAILOVER, STT & AI PIPELINE")
    check("Separate Medya/FFmpeg tab removed from HTML (3 tabs only)",
          'id="panel-media"' not in html_txt and 'data-tab="media"' not in html_txt
          and all(t in html_txt for t in ('id="panel-tts"', 'id="panel-ai"', 'id="panel-legal"')))

    # Strict monthly 2 image quota (no daily limit blocking 2 on same day, 3rd blocked)
    r_iq = client.post("/api/auth/register", json={
        "username": "MonthlyImgUser", "password": good_pw, "password_confirm": good_pw
    })
    iq_tok = r_iq.get_json()["token"]
    iq_hdrs = {"Authorization": f"Bearer {iq_tok}"}
    fake_png_b64 = base64.b64encode(Path(imgs[0]).read_bytes()).decode("ascii")

    with mock.patch.object(ai_engine, "_pollinations_image", return_value=(fake_png_b64, "image/png", "pollinations/flux")):
        r_img1 = client.post("/api/image", json={"prompt": "aslan 1"}, headers=iq_hdrs)
        r_img2 = client.post("/api/image", json={"prompt": "aslan 2"}, headers=iq_hdrs)
        r_img3 = client.post("/api/image", json={"prompt": "aslan 3"}, headers=iq_hdrs)
    check("Monthly image 1/2 succeeds (remaining=1)",
          r_img1.status_code == 200 and r_img1.get_json()["quota"]["image"]["monthly_remaining"] == 1)
    check("Monthly image 2/2 same day succeeds (remaining=0)",
          r_img2.status_code == 200 and r_img2.get_json()["quota"]["image"]["monthly_remaining"] == 0)
    check("Monthly image 3/2 blocked → 429",
          r_img3.status_code == 429 and r_img3.get_json()["quota"]["image"]["allowed"] is False)

    # Month rollover resets quota back to 2
    u_rec = database.get_user_by_username("MonthlyImgUser")
    u_rec["quota"]["image"]["month"]["month"] = "2020-01"
    database.save_user_record(u_rec)
    r_q_roll = client.get("/api/quota", headers=iq_hdrs)
    check("New calendar month resets image quota back to 2/2",
          r_q_roll.status_code == 200 and r_q_roll.get_json()["image"]["monthly_remaining"] == 2)

    # Free image endpoint priority + Gemini last resort
    gemini_called = []

    def fake_gem_post(url, payload, timeout=90, headers=None):
        gemini_called.append((url, headers or {}))
        return {"predictions": [{"bytesBase64Encoded": fake_png_b64, "mimeType": "image/png"}]}

    with mock.patch.object(ai_engine, "_pollinations_image", return_value=(fake_png_b64, "image/png", "pollinations/flux")), \
         mock.patch.object(ai_engine, "_post_json", side_effect=fake_gem_post):
        ok_pf, res_pf = ai_engine.generate_image("kedi", username_lower="monthlyimguser", api_key="AIzaDummyImgKey")
    check("Free image provider tried FIRST (Gemini skipped when Pollinations works)",
          ok_pf and str(res_pf.get("engine", "")).startswith("pollinations") and len(gemini_called) == 0)

    with mock.patch.object(ai_engine, "_pollinations_image", return_value=(None, None, None)), \
         mock.patch.object(ai_engine, "_try_free_image_endpoints", return_value=(None, None)), \
         mock.patch.object(ai_engine, "_post_json", side_effect=fake_gem_post):
        ok_gf, res_gf = ai_engine.generate_image("kopek", username_lower="monthlyimguser", api_key="AIzaDummyImgKey")
    check("Gemini image acts as LAST RESORT when all free image providers fail",
          ok_gf and len(gemini_called) > 0 and gemini_called[0][1].get("x-goog-api-key") == "AIzaDummyImgKey")

    # New FFmpeg capabilities: add_text (img & video), image_audio_video, apply_filter, burn_subtitles
    ok_ti, p_ti = media_engine.execute_media_operation(
        "add_text", [imgs[0]], WORK / "ti.png", {"text": "ASLAN Şampiyon 2026", "position": "bottom"}
    )
    check("FFmpeg/Pillow add_text on image succeeds",
          ok_ti and Path(p_ti).suffix == ".png" and Path(p_ti).stat().st_size > 500)

    ok_tv, p_tv = media_engine.execute_media_operation(
        "add_text", [str(vid)], WORK / "tv.mp4", {"text": "Türkçe Başlık", "position": "top"}
    )
    check("FFmpeg add_text on video succeeds",
          ok_tv and Path(p_tv).suffix == ".mp4" and Path(p_tv).stat().st_size > 1000)

    ok_iav, p_iav = media_engine.execute_media_operation(
        "image_audio_video", [imgs[0], str(aud)], WORK / "iav.mp4", {}
    )
    info_iav = media_engine.get_media_info(str(p_iav)) if ok_iav else None
    types_iav = [s.get("codec_type") for s in (info_iav or {}).get("streams", [])]
    check("FFmpeg image_audio_video produces video+audio MP4",
          ok_iav and "video" in types_iav and "audio" in types_iav, str(types_iav))

    ok_flt, p_flt = media_engine.execute_media_operation(
        "apply_filter", [imgs[0]], WORK / "flt.png", {"filter": "grayscale"}
    )
    check("FFmpeg apply_filter grayscale succeeds",
          ok_flt and Path(p_flt).stat().st_size > 300)

    ok_sub, p_sub = media_engine.execute_media_operation(
        "burn_subtitles", [str(vid)], WORK / "sub.mp4",
        {"srt": "1\n00:00:00,000 --> 00:00:01,500\nMerhaba Aslan!\n"}
    )
    check("FFmpeg burn_subtitles succeeds",
          ok_sub and Path(p_sub).stat().st_size > 1000)

    # Multi-step AI pipeline: generate 2 images -> combine with audio into MP4 video
    r_pipe_u = client.post("/api/auth/register", json={
        "username": "PipeUser2", "password": good_pw, "password_confirm": good_pw
    })
    pipe_tok = r_pipe_u.get_json()["token"]
    pipe_hdrs = {"Authorization": f"Bearer {pipe_tok}"}
    aud_b64 = base64.b64encode(Path(aud).read_bytes()).decode("ascii")
    pipe_reply = (
        'İki görseli üretip müzikli videoya dönüştürdüm.\n'
        '```aslan_action\n'
        '{"steps":['
        '{"id":"img1","action":"generate_image","params":{"prompt":"kirmizi aslan"}},'
        '{"id":"img2","action":"generate_image","params":{"prompt":"altin aslan"}},'
        '{"id":"vid1","action":"slideshow_with_music","files":["$img1","$img2","$last_audio"],"params":{"per_image_duration":2,"transition":"fade"}}'
        ']}\n```'
    )
    with mock.patch.object(ai_engine, "generate_reply", return_value=(pipe_reply, None)), \
         mock.patch.object(ai_engine, "_pollinations_image", return_value=(fake_png_b64, "image/png", "pollinations/flux")):
        r_pipe = client.post("/api/ai", json={
            "message": "İki görsel üret, birleştirip müzikli video yap",
            "files": [{"name": "bg.mp3", "mime_type": "audio/mpeg", "data": aud_b64}],
        }, headers=pipe_hdrs)
    pj = r_pipe.get_json() or {}
    check("AI multi-step pipeline (2 images + audio -> video/mp4) → 200",
          r_pipe.status_code == 200
          and len(pj.get("generated_images") or []) == 2
          and (pj.get("media_result") or {}).get("success") is True
          and (pj.get("media_result") or {}).get("mime_type") == "video/mp4"
          and (pj.get("quota") or {}).get("image", {}).get("monthly_remaining") == 0)

    # When 0/2 image quota remains, 2-image pipeline preflight fails cleanly
    with mock.patch.object(ai_engine, "generate_reply", return_value=(pipe_reply, None)):
        r_pipe_over = client.post("/api/ai", json={"message": "2 görsel daha yap"}, headers=pipe_hdrs)
    check("AI multi-step pipeline preflight rejects when monthly 2-image quota exhausted",
          r_pipe_over.status_code == 200
          and (r_pipe_over.get_json().get("media_result") or {}).get("quota_exceeded") is True)

    # STT endpoint & frontend check
    check("Unauthenticated /api/stt → 401", client.post("/api/stt", json={"audio_base64": "AA=="}).status_code == 401)
    with mock.patch.object(ai_engine, "transcribe_audio", return_value=("Merhaba Aslan AI", None)):
        r_stt = client.post("/api/stt", json={"audio_base64": aud_b64, "mime_type": "audio/mpeg"}, headers=pipe_hdrs)
        v_stt = FakeHandler(
            api_index.handler, "POST", path="/api/index.py?__route=/api/stt",
            body=json.dumps({"audio_base64": aud_b64, "mime_type": "audio/mpeg"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {pipe_tok}"},
        ).run()
    check("Flask & Vercel /api/stt return transcribed text → 200",
          r_stt.status_code == 200 and r_stt.get_json().get("text") == "Merhaba Aslan AI"
          and v_stt.status == 200 and v_stt.json().get("text") == "Merhaba Aslan AI")
    check("Frontend bundle includes Web Speech API & /api/stt fallback",
          "SpeechRecognition" in js_txt and "/api/stt" in js_txt)
    ai_engine_src = (ROOT / "core" / "ai_engine.py").read_text(encoding="utf-8")
    check("yt-dlp configured with cachedir=False & worker client available",
          "'cachedir': False" in ai_engine_src and (ROOT / "core" / "worker_client.py").is_file())

    # ------------------------------------------------------------------
    section("11. ARAYUZ GIZLILIGI & SADECE ASLAN AI MARKASI")
    built_texts = {
        "index.html": html_txt,
        "app.min.js": js_txt,
        "app.min.css": css_txt,
        "manifest.json": (ROOT / "public" / "manifest.json").read_text(encoding="utf-8"),
        "sw.js": sw_txt,
    }
    forbidden_ui_tokens = (
        "gemini", "pollinations", "ffmpeg", "ffprobe", "yt-dlp", "yt_dlp", "ytdlp",
        "baileys", "whiskeysockets", "edge-tts", "edge_tts", "gtts", "imagen",
        "nano-banana", "pillow", "openai", "mistral", "deepseek", "claude", "llama",
        "groq", "huggingface", "neural tts", "pbkdf2", "sunucusuz",
        "social-studio", "video indir", "opensocialstudio", "downloadsocial",
    )
    leaks = []
    for fname, body in built_texts.items():
        low = body.lower()
        for tok in forbidden_ui_tokens:
            if tok in low:
                leaks.append(f"{fname}:{tok}")
    check("Built UI/manifest/SW contain zero backend library, model or API names",
          len(leaks) == 0, ", ".join(leaks))
    check("Built HTML omits 'API' wording from visible interface",
          not re.search(r'\bapi\b', html_txt, re.IGNORECASE))

    sample_raw_errors = [
        "gemini-2.5-flash quota exceeded",
        "HTTPSConnectionPool host='text.pollinations.ai': https://text.pollinations.ai/openai",
        "ffmpeg: libx264 failed with code 1",
        "ERROR: [youtube] yt-dlp could not extract",
        "Baileys connection closed unexpectedly",
        "edge-tts failed: 403 NoAudioReceived",
        "gTTS error connecting to translate",
        "imagen-3.0-fast-generate-001 returned empty predictions",
        "Secret leak sk-test-secret-key-1234567890 occurred",
    ]
    scrubber_ok = True
    scrubber_bad = []
    for raw_err in sample_raw_errors:
        cleaned = api_utils.safe_error_message(raw_err)
        clow = cleaned.lower()
        for kw in ("gemini", "pollinations", "ffmpeg", "libx264", "yt-dlp", "baileys", "edge-tts", "gtts", "imagen", "http", "sk-test-secret"):
            if kw in clow:
                scrubber_ok = False
                scrubber_bad.append(f"{kw} in {cleaned!r}")
    check("safe_error_message scrubs backend libraries, models and URLs",
          scrubber_ok, "; ".join(scrubber_bad))

    # Endpoint response / header masking (Flask + Vercel)
    r_brand_u = client.post("/api/auth/register", json={
        "username": "BrandMaskUser", "password": good_pw, "password_confirm": good_pw
    })
    brand_hdrs = {"Authorization": f"Bearer {r_brand_u.get_json()['token']}"}
    with mock.patch.object(ai_engine, "_pollinations_image", return_value=(fake_png_b64, "image/png", "pollinations/flux")):
        r_f_img = client.post("/api/image", json={"prompt": "gizli marka testi"}, headers=brand_hdrs)
        v_f_img = FakeHandler(
            api_index.handler, "POST", path="/api/index.py?__route=/api/image",
            body=json.dumps({"prompt": "gizli marka testi"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {v_tok}"},
        ).run()
    r_v_img_check = (
        r_f_img.status_code == 200 and r_f_img.get_json().get("engine") == "Aslan AI"
        and v_f_img.status == 200 and v_f_img.json().get("engine") == "Aslan AI"
    )
    check("/api/image (Flask & Vercel) masks upstream engine as 'Aslan AI'",
          r_v_img_check, f"flask={r_f_img.status_code}:{r_f_img.get_json()} vercel={v_f_img.status}:{v_f_img.json()}")

    with mock.patch.object(ai_engine, "synthesize_speech", return_value=(b"ID3testaudio", "edge-tts")):
        r_f_tts = client.post("/api/tts", json={"text": "Merhaba Aslan AI"}, headers=brand_hdrs)
        v_f_tts = FakeHandler(
            api_index.handler, "POST", path="/api/index.py?__route=/api/tts",
            body=json.dumps({"text": "Merhaba Aslan AI"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {v_tok}"},
        ).run()
    v_tts_hdrs = {k.lower(): v[-1] for k, v in v_f_tts.header_map().items()}
    check("/api/tts (Flask & Vercel) sets X-Engine header to 'Aslan AI'",
          r_f_tts.status_code == 200 and r_f_tts.headers.get("X-Engine") == "Aslan AI"
          and v_f_tts.status == 200 and v_tts_hdrs.get("x-engine") == "Aslan AI")

    pipe_imgs = pj.get("generated_images") or []
    check("/api/ai multi-step pipeline tags images as 'Aslan AI'",
          len(pipe_imgs) == 2 and all(item.get("engine") == "Aslan AI" for item in pipe_imgs))

    sys_prompt = ai_engine.build_system_prompt()
    check("System prompt hides FFmpeg/yt-dlp labels and enforces Aslan AI identity",
          "FFmpeg Medya Motoru" not in sys_prompt
          and "(yt-dlp)" not in sys_prompt
          and "ASLAN SITE OZELLIKLERI" in sys_prompt
          and "ASLA soyleme" in sys_prompt)

    # ------------------------------------------------------------------
    section("12. GIZLI VIDEO INDIRME & WHATSAPP BOT OLUSTURUCU (BAILEYS IPv4 + TEK HAK)")
    import socket
    import sqlite3
    import time
    import urllib.request

    check("Video download UI completely hidden from HTML & JS bundle",
          'id="social-studio"' not in html_txt
          and "Video İndir" not in html_txt
          and "openSocialStudio" not in js_txt
          and "downloadSocial" not in js_txt)
    check("Silent video download instructions & action remain active in backend AI prompt",
          "Video indirebiliyor musun?" in sys_prompt and "social_download" in sys_prompt)

    wa_dir = ROOT / "worker" / "wa_service"
    for wa_file in ("ipv4.mjs", "bot_runtime.mjs", "server.mjs"):
        wchk = subprocess.run(["node", "--check", str(wa_dir / wa_file)], capture_output=True, text=True)
        check(f"WA service {wa_file} syntax valid", wchk.returncode == 0, wchk.stderr[:180])

    ipv4_src = (wa_dir / "ipv4.mjs").read_text(encoding="utf-8")
    check("WA service forces IPv4 (ipv4first & family: 4)",
          "setDefaultResultOrder('ipv4first')" in ipv4_src and "family: 4" in ipv4_src)

    baileys_imp = subprocess.run(
        ["node", "--input-type=module", "-e",
         "import m, { useMultiFileAuthState, fetchLatestBaileysVersion, Browsers } from '@whiskeysockets/baileys'; console.log('OK', typeof (m?.default || m), typeof useMultiFileAuthState, typeof fetchLatestBaileysVersion, typeof Browsers);"],
        cwd=str(wa_dir), capture_output=True, text=True,
    )
    check("@whiskeysockets/baileys installed and ESM-importable",
          baileys_imp.returncode == 0 and "OK function function function" in baileys_imp.stdout,
          (baileys_imp.stdout + baileys_imp.stderr)[:180])

    # Free ephemeral port for Node WA worker in mock mode
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        wa_port = s.getsockname()[1]

    wa_token = "test-wa-worker-token-2026"
    wa_env = os.environ.copy()
    wa_env.update({
        "WA_WORKER_HOST": "127.0.0.1",
        "WA_WORKER_PORT": str(wa_port),
        "WA_BOT_WORKER_TOKEN": wa_token,
        "WA_MOCK_MODE": "1",
        "WA_AUTH_DIR": str(WORK / "wa_auth"),
    })
    wa_proc = subprocess.Popen(
        ["node", "server.mjs"],
        cwd=str(wa_dir),
        env=wa_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    old_wa_url = os.environ.get("WA_BOT_WORKER_URL")
    old_wa_tok = os.environ.get("WA_BOT_WORKER_TOKEN")
    os.environ["WA_BOT_WORKER_URL"] = f"http://127.0.0.1:{wa_port}"
    os.environ["WA_BOT_WORKER_TOKEN"] = wa_token
    try:
        worker_up = False
        for _ in range(80):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{wa_port}/health", timeout=1) as resp:
                    if resp.status == 200 and json.loads(resp.read().decode("utf-8")).get("ipv4") is True:
                        worker_up = True
                        break
            except Exception:
                time.sleep(0.25)
        check("Spawned WA Baileys IPv4 service responds /health", worker_up)
        with urllib.request.urlopen(f"http://127.0.0.1:{wa_port}/ping", timeout=2) as p_resp:
            p_json = json.loads(p_resp.read().decode("utf-8"))
        check("Spawned WA Baileys IPv4 service responds /ping", p_resp.status == 200 and p_json.get("ok") is True)

        # 12.1 Risk rejection stops setup cleanly
        r_rj_u = client.post("/api/auth/register", json={
            "username": "WaRejectUser", "password": good_pw, "password_confirm": good_pw
        })
        rj_hdrs = {"Authorization": f"Bearer {r_rj_u.get_json()['token']}"}
        r_rj1 = client.post("/api/ai", json={"message": "WhatsApp botu yapabilir misin?"}, headers=rj_hdrs)
        r_rj2 = client.post("/api/ai", json={"message": "Hayır, kabul etmiyorum"}, headers=rj_hdrs)
        rj_state = database.get_user_wa_bot("warejectuser")
        check("WA setup stops immediately if user rejects risk disclaimer",
              r_rj1.status_code == 200 and "sorumluluk" in r_rj1.get_json().get("reply", "").lower()
              and r_rj2.status_code == 200 and rj_state.get("stage") == "idle"
              and not rj_state.get("setup_session", {}).get("active"))

        # 12.2 Happy path: 10+ turns total cost == 1 credit + pairing card + DB encrypted key + live update
        r_hp_u = client.post("/api/auth/register", json={
            "username": "WaHappyUser", "password": good_pw, "password_confirm": good_pw
        })
        hp_hdrs = {"Authorization": f"Bearer {r_hp_u.get_json()['token']}"}

        t1 = client.post("/api/ai", json={"message": "WhatsApp botu yapabilir misin?"}, headers=hp_hdrs)
        check("WA Turn 1: risk warning & disclaimer, consumes 1 credit (4/5 left)",
              t1.status_code == 200 and "sorumluluk" in t1.get_json()["reply"].lower()
              and t1.get_json()["quota"]["ai"]["remaining"] == 4)

        t2 = client.post("/api/ai", json={"message": "Onaylıyorum"}, headers=hp_hdrs)
        check("WA Turn 2: asks phone number (0 extra credits, still 4/5)",
              t2.status_code == 200 and "telefon numaras" in t2.get_json()["reply"].lower()
              and t2.get_json()["quota"]["ai"]["remaining"] == 4)

        t3 = client.post("/api/ai", json={"message": "+90 555 123 45 67"}, headers=hp_hdrs)
        check("WA Turn 3: asks explicit phone usage permission (still 4/5)",
              t3.status_code == 200 and "izin veriyor musunuz" in t3.get_json()["reply"].lower()
              and t3.get_json()["quota"]["ai"]["remaining"] == 4)

        t4 = client.post("/api/ai", json={"message": "İzin veriyorum"}, headers=hp_hdrs)
        check("WA Turn 4: warns short expiry & asks 'Kodu almayı onaylıyor musunuz?' (still 4/5)",
              t4.status_code == 200 and "kodu almayı onaylıyor musunuz" in t4.get_json()["reply"].lower()
              and t4.get_json()["quota"]["ai"]["remaining"] == 4)

        t5 = client.post("/api/ai", json={"message": "Evet, kodu almayı onaylıyorum"}, headers=hp_hdrs)
        t5j = t5.get_json() or {}
        ev5 = t5j.get("wa_event") or {}
        check("WA Turn 5: returns 8-char pairing code & instructions (still 4/5)",
              t5.status_code == 200 and ev5.get("type") == "pairing_code"
              and len(ev5.get("code", "")) == 8
              and "Bağlı Cihazlar" in t5j.get("reply", "")
              and t5j["quota"]["ai"]["remaining"] == 4)

        # Simulate WhatsApp connection open on worker
        mc_req = urllib.request.Request(
            f"http://127.0.0.1:{wa_port}/wa/mock-connect",
            data=json.dumps({"user_id": "wahappyuser"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Worker-Token": wa_token},
            method="POST",
        )
        with urllib.request.urlopen(mc_req, timeout=5) as mc_resp:
            mc_data = json.loads(mc_resp.read().decode("utf-8"))
        check("WA mock connection succeeded on Node Baileys worker", mc_data.get("connected") is True)

        st_f = client.get("/api/wa/status", headers=hp_hdrs)
        st_v = FakeHandler(
            api_index.handler, "GET", path="/api/index.py?__route=/api/wa/status",
            headers=hp_hdrs,
        ).run()
        check("/api/wa/status (Flask & Vercel) reports connected & 'Bağlantınız başarılı'",
              st_f.status_code == 200 and st_f.get_json().get("connected") is True
              and st_f.get_json().get("notification") == "Bağlantınız başarılı"
              and st_v.status == 200 and st_v.json().get("connected") is True
              and st_v.json().get("notification") == "Bağlantınız başarılı")

        cf = client.post("/api/wa/confirm-connected", json={}, headers=hp_hdrs)
        check("POST /api/wa/confirm-connected starts Q1 customization (0 credits)",
              cf.status_code == 200 and "1/5" in (cf.get_json() or {}).get("reply", ""))

        t6 = client.post("/api/ai", json={"message": "Tüm mesajlara cevap versin ama reklam kelimelerine sessiz kalsın"}, headers=hp_hdrs)
        t7 = client.post("/api/ai", json={"message": "Özel mesajlarda cevap versin, gruplarda cevap vermesin"}, headers=hp_hdrs)
        t8 = client.post("/api/ai", json={"message": "3 saniye beklesin ve yazıyor göstersin"}, headers=hp_hdrs)
        t9 = client.post("/api/ai", json={"message": "Samimi olsun; teşekkür -> ❤️; fiyat -> Güncel liste 500 TL"}, headers=hp_hdrs)
        user_ext_key = "AIzaUserOwnCustomKey987654321"
        t10 = client.post("/api/ai", json={"message": f"Evet, Gemini anahtarım: {user_ext_key}"}, headers=hp_hdrs)
        t10j = t10.get_json() or {}

        with sqlite3.connect(database._db_path()) as conn:
            raw_wa_row = conn.execute(
                "SELECT wa_bot_json FROM users WHERE username_lower = ?", ("wahappyuser",)
            ).fetchone()
        raw_wa_json_str = raw_wa_row[0] if raw_wa_row else ""

        w_st_req = urllib.request.Request(
            f"http://127.0.0.1:{wa_port}/wa/status?user_id=wahappyuser",
            headers={"X-Worker-Token": wa_token},
            method="GET",
        )
        with urllib.request.urlopen(w_st_req, timeout=5) as w_st_resp:
            w_st = json.loads(w_st_resp.read().decode("utf-8"))

        check("WA customization Q1-Q5 + encrypted custom AI key completes with TOTAL 1 credit (4/5 left)",
              all(r.status_code == 200 and r.get_json()["quota"]["ai"]["remaining"] == 4 for r in (t6, t7, t8, t9, t10))
              and (t10j.get("wa_event") or {}).get("type") == "bot_ready"
              and user_ext_key not in t10j.get("reply", "")
              and user_ext_key not in raw_wa_json_str
              and (w_st.get("config_summary") or {}).get("custom_ai", {}).get("has_key") is True
              and (w_st.get("config_summary") or {}).get("custom_ai", {}).get("enabled") is True)

        # 12.3 User starting with ONLY 1 credit remaining finishes entire flow
        r_one_u = client.post("/api/auth/register", json={
            "username": "WaOneCreditUser", "password": good_pw, "password_confirm": good_pw
        })
        one_hdrs = {"Authorization": f"Bearer {r_one_u.get_json()['token']}"}
        database.consume_user_ai_quota("waonecredituser", amount=4)
        o1 = client.post("/api/ai", json={"message": "WhatsApp botu yapabilir misin?"}, headers=one_hdrs)
        o2 = client.post("/api/ai", json={"message": "Onaylıyorum"}, headers=one_hdrs)
        o3 = client.post("/api/ai", json={"message": "05559876543"}, headers=one_hdrs)
        o4 = client.post("/api/ai", json={"message": "İzin veriyorum"}, headers=one_hdrs)
        o5 = client.post("/api/ai", json={"message": "Kodu almayı onaylıyorum"}, headers=one_hdrs)
        mc_one = urllib.request.Request(
            f"http://127.0.0.1:{wa_port}/wa/mock-connect",
            data=json.dumps({"user_id": "waonecredituser"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Worker-Token": wa_token},
            method="POST",
        )
        with urllib.request.urlopen(mc_one, timeout=5):
            pass
        o_cf = client.post("/api/wa/confirm-connected", json={}, headers=one_hdrs)
        o6 = client.post("/api/ai", json={"message": "Sipariş mesajlarına cevap ver"}, headers=one_hdrs)
        o7 = client.post("/api/ai", json={"message": "Sadece özel mesaj"}, headers=one_hdrs)
        o8 = client.post("/api/ai", json={"message": "2 saniye yazıyor açık"}, headers=one_hdrs)
        o9 = client.post("/api/ai", json={"message": "Kibar ve net"}, headers=one_hdrs)
        o10 = client.post("/api/ai", json={"message": "Hayır, sadece kurallara göre çalışsın"}, headers=one_hdrs)
        check("User with last 1/5 credit completes full rule-based WA bot setup cleanly",
              all(r.status_code == 200 for r in (o1, o2, o3, o4, o5, o_cf, o6, o7, o8, o9, o10))
              and o1.get_json()["quota"]["ai"]["remaining"] == 0
              and (o10.get_json().get("wa_event") or {}).get("type") == "bot_ready")

        # 12.4 Live update on active bot via chat
        lu = client.post(
            "/api/ai",
            json={"message": "Artık gruplarda cevap vermesin, sadece 'sipariş' kelimesi geçince yanıt versin"},
            headers=hp_hdrs,
        )
        luj = lu.get_json() or {}
        lu_sum = (luj.get("wa_event") or {}).get("config_summary") or {}
        with urllib.request.urlopen(w_st_req, timeout=5) as w_st_after:
            w_after = json.loads(w_st_after.read().decode("utf-8"))
        check("Live bot update modifies allow_groups/trigger_keywords and syncs to worker",
              lu.status_code == 200
              and lu_sum.get("allow_groups") is False
              and "sipariş" in (lu_sum.get("trigger_keywords") or [])
              and w_after.get("config_updates_count", 0) >= 2
              and "sipariş" in ((w_after.get("config_summary") or {}).get("trigger_keywords") or []))

        # 12.5 Disconnect -> 60s Keep-Alive auto-reconnect verification
        md_req = urllib.request.Request(
            f"http://127.0.0.1:{wa_port}/wa/mock-disconnect",
            data=json.dumps({"user_id": "wahappyuser"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Worker-Token": wa_token},
            method="POST",
        )
        with urllib.request.urlopen(md_req, timeout=5) as md_resp:
            md_data = json.loads(md_resp.read().decode("utf-8"))
        with urllib.request.urlopen(f"http://127.0.0.1:{wa_port}/ping", timeout=3) as wake_resp:
            wake_ok = wake_resp.status == 200
        with urllib.request.urlopen(w_st_req, timeout=5) as rec_resp:
            rec_data = json.loads(rec_resp.read().decode("utf-8"))
        check("60s keep-alive /ping automatically reconnects dropped WhatsApp bot session",
              md_data.get("connected") is False
              and wake_ok
              and rec_data.get("connected") is True
              and rec_data.get("reconnect_count", 0) >= 1)

        # 12.6 Frontend pairing & copy card present in built bundle
        check("Frontend bundle & CSS include WA pairing copy button & live status polling",
              "copy-wa-code" in js_txt
              and "confirm-wa-connected" in js_txt
              and "/api/wa/status" in js_txt
              and "Bağlantınız başarılı" in js_txt
              and ".wa-pair-code" in css_txt
              and ".wa-copy-btn" in css_txt)

        # 12.7 Expanded Turkish confirmations & natural phrases
        from core import wa_bot, keepalive
        check("WA _is_affirmative accepts natural phrases and blocks negatives",
              all(wa_bot._is_affirmative(x) for x in ("Kodu al", "Kodu gönder", "Tamam, başlat", "Olur", "Onaylıyorum"))
              and not any(wa_bot._is_affirmative(x) for x in ("Vazgectim", "İptal", "Hayır kodu gönderme")))

        # 12.8 Built-in Vercel WA bridge URL, shared HMAC token, & keepalive target resolution
        env_patch = {
            "VERCEL": "1",
            "VERCEL_ENV": "production",
            "WA_BOT_WORKER_URL": "",
            "WA_BOT_WORKER_TOKEN": "",
            "MEDIA_WORKER_TOKEN": "",
            "QUOTA_SIGNING_SECRET": "bridge-test-hmac-seed",
            "VERCEL_PROJECT_PRODUCTION_URL": "",
        }
        with mock.patch.dict(os.environ, env_patch, clear=False), \
             mock.patch.object(cfg, "WA_BOT_WORKER_URL", ""), \
             mock.patch.object(cfg, "WA_BOT_WORKER_TOKEN", ""):
            v_bridge_url, v_bridge_tok = wa_bot.get_worker_endpoint()
            ka_targets = keepalive._collect_worker_targets()
        check("Vercel mode resolves built-in /api/wa_bridge endpoint, HMAC token, & keepalive target",
              v_bridge_url == "https://aslansite.vercel.app/api/wa_bridge"
              and len(v_bridge_tok) == 64
              and ("https://aslansite.vercel.app/api/wa_bridge/ping", v_bridge_tok) in ka_targets)

        # 12.9 Direct ESM fetch() handler test of api/wa_bridge.mjs
        bridge_script = (
            "import bridge from './api/wa_bridge.mjs';\n"
            "const unauthH = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/health'));\n"
            "const unauthS = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/status?user_id=u1'));\n"
            "const tok = process.env.TEST_BRIDGE_TOK;\n"
            "const hRes = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/health', { headers: { 'x-worker-token': tok } }));\n"
            "const hJson = await hRes.json();\n"
            "const pRes = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/pair', { method: 'POST', headers: { 'x-worker-token': tok, 'content-type': 'application/json' }, body: JSON.stringify({ user_id: 'bridgeuser', phone: '905551234567', config: {} }) }));\n"
            "const pJson = await pRes.json();\n"
            "const mcRes = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/mock-connect', { method: 'POST', headers: { 'x-worker-token': tok, 'content-type': 'application/json' }, body: JSON.stringify({ user_id: 'bridgeuser' }) }));\n"
            "const sRes = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/status?user_id=bridgeuser', { headers: { 'x-worker-token': tok } }));\n"
            "const sJson = await sRes.json();\n"
            "const cRes = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/config', { method: 'POST', headers: { 'x-worker-token': tok, 'content-type': 'application/json' }, body: JSON.stringify({ user_id: 'bridgeuser', config: { allow_groups: true, trigger_keywords: ['fiyat'] } }) }));\n"
            "const cJson = await cRes.json();\n"
            "const pgRes = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/ping', { headers: { 'x-worker-token': tok } }));\n"
            "const pgJson = await pgRes.json();\n"
            "const stRes = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/stop', { method: 'POST', headers: { 'x-worker-token': tok, 'content-type': 'application/json' }, body: JSON.stringify({ user_id: 'bridgeuser', logout: true }) }));\n"
            "const stJson = await stRes.json();\n"
            "console.log(JSON.stringify({ unauthH: unauthH.status, unauthS: unauthS.status, hSt: hRes.status, hJson, pSt: pRes.status, pJson, mcSt: mcRes.status, sSt: sRes.status, sJson, cSt: cRes.status, cJson, pgSt: pgRes.status, pgJson, stSt: stRes.status, stJson }));\n"
        )
        b_env = os.environ.copy()
        b_env.pop("WA_BOT_WORKER_TOKEN", None)
        b_env.pop("MEDIA_WORKER_TOKEN", None)
        b_env["QUOTA_SIGNING_SECRET"] = "bridge-test-hmac-seed"
        b_env["WA_MOCK_MODE"] = "1"
        b_env["TEST_BRIDGE_TOK"] = v_bridge_tok
        b_run = subprocess.run(
            ["node", "--input-type=module", "-e", bridge_script],
            cwd=str(ROOT), env=b_env, capture_output=True, text=True,
        )
        b_out = {}
        if b_run.returncode == 0 and b_run.stdout.strip():
            try:
                b_out = json.loads(b_run.stdout.strip().splitlines()[-1])
            except Exception:
                b_out = {}
        check("Vercel api/wa_bridge.mjs fetch() handles auth, pair, mock-connect, status, config, ping & stop",
              b_run.returncode == 0
              and b_out.get("unauthH") == 401
              and b_out.get("unauthS") == 401
              and b_out.get("hSt") == 200 and b_out.get("hJson", {}).get("ipv4") is True
              and b_out.get("pSt") == 200 and len(b_out.get("pJson", {}).get("pairing_code", "")) == 8
              and b_out.get("mcSt") == 200 and b_out.get("sSt") == 200 and b_out.get("sJson", {}).get("connected") is True
              and b_out.get("cSt") == 200 and "fiyat" in (b_out.get("cJson", {}).get("config", {}).get("trigger_keywords") or [])
              and b_out.get("pgSt") == 200 and b_out.get("pgJson", {}).get("ok") is True
              and b_out.get("stSt") == 200 and b_out.get("stJson", {}).get("stopped") is True,
              (b_run.stdout + b_run.stderr)[:220])

        # 12.10 Unconfigured warning completely removed, writable /tmp storage & flexible Step 4 phrases
        wa_bot_src = (ROOT / "core" / "wa_bot.py").read_text(encoding="utf-8")
        wa_bridge_src = (ROOT / "api" / "wa_bridge.mjs").read_text(encoding="utf-8")
        wa_rt_check_src = (wa_dir / "bot_runtime.mjs").read_text(encoding="utf-8")
        check("WA modules completely remove 'yapılandırılmamış' fallback and use os.tmpdir() on Vercel",
              "yapılandırılmamış" not in wa_bot_src.lower()
              and "yapilandirilmamis" not in wa_bot_src.lower()
              and "yapılandırılmamış" not in wa_bridge_src.lower()
              and "os.tmpdir()" in wa_bridge_src
              and "aslan_wa_sessions" in wa_bridge_src
              and "os.tmpdir()" in wa_rt_check_src)

        flex_words = ("izin veriyorum", "evet", "tamam", "kabul ediyorum", "onaylıyorum")
        flex_ok = True
        flex_bad = ""
        for idx_fw, fw in enumerate(flex_words):
            r_fu = client.post("/api/auth/register", json={
                "username": f"WaFlex{idx_fw}User", "password": good_pw, "password_confirm": good_pw
            })
            f_hdrs = {"Authorization": f"Bearer {r_fu.get_json()['token']}"}
            f1 = client.post("/api/ai", json={"message": "WhatsApp botu yapabilir misin?"}, headers=f_hdrs)
            f2 = client.post("/api/ai", json={"message": "Onaylıyorum"}, headers=f_hdrs)
            f3 = client.post("/api/ai", json={"message": "+90 555 000 11 22"}, headers=f_hdrs)
            f4 = client.post("/api/ai", json={"message": "İzin veriyorum"}, headers=f_hdrs)
            f5 = client.post("/api/ai", json={"message": fw}, headers=f_hdrs)
            f5j = f5.get_json() or {}
            ev5f = f5j.get("wa_event") or {}
            if not (f1.status_code == 200 and f2.status_code == 200 and f3.status_code == 200
                    and f4.status_code == 200 and f5.status_code == 200
                    and ev5f.get("type") == "pairing_code"
                    and len(ev5f.get("code", "")) == 8
                    and f5j["quota"]["ai"]["remaining"] == 4):
                flex_ok = False
                flex_bad = f"{fw} -> {f5j}"
                break
        check("Step 4 flexible confirmations (izin veriyorum/evet/tamam/kabul ediyorum/onaylıyorum) generate 8-char code (total 1 credit)",
              flex_ok, flex_bad)

        # 12.11 Serverless wake-on-message, idle sleep, Baileys ws.isOpen/socket.ping & single-chain auto-wake
        check("Bridge & runtime use WebSocketClient ws.isOpen/socket.ping, wake lease, receivedPendingNotifications & adaptive sleep",
              "ws.isOpen" in wa_bridge_src and "ws.socket.ping" in wa_bridge_src
              and "ws.isOpen" in wa_rt_check_src and "ws.socket.ping" in wa_rt_check_src
              and "wake_lease" in wa_bridge_src and "receivedPendingNotifications" in wa_bridge_src
              and all(k in wa_bridge_src for k in (
                  "WA_ACTIVE_SLEEP_MS", "WA_IDLE_SLEEP_INTERVAL_MS", "WA_DEEP_SLEEP_INTERVAL_MS",
                  "evalRes", "evaluateIncomingMessage", "recentMessageIds",
              )))

        wake_script = (
            "import bridge from './api/wa_bridge.mjs';\n"
            "const tok = process.env.TEST_BRIDGE_TOK;\n"
            "const unauthW = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/wake', { method: 'POST', body: '{}' }));\n"
            "const authW = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/wake', { method: 'POST', headers: { 'x-worker-token': tok, 'content-type': 'application/json' }, body: JSON.stringify({ user_id: 'wakeuser', force: true }) }));\n"
            "const authWj = await authW.json();\n"
            "await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/pair', { method: 'POST', headers: { 'x-worker-token': tok, 'content-type': 'application/json' }, body: JSON.stringify({ user_id: 'wakeuser', phone: '905551112233', config: {} }) }));\n"
            "await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/mock-connect', { method: 'POST', headers: { 'x-worker-token': tok, 'content-type': 'application/json' }, body: JSON.stringify({ user_id: 'wakeuser' }) }));\n"
            "await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/config', { method: 'POST', headers: { 'x-worker-token': tok, 'content-type': 'application/json' }, body: JSON.stringify({ user_id: 'wakeuser', config: { allow_groups: false, allow_private: true, rules: [{ trigger: 'fiyat', response: 'Güncel fiyat 500 TL', match_type: 'contains' }], auto_reactions: [{ trigger: 'teşekkür', emoji: '❤️' }] } }) }));\n"
            "const inc1 = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/mock-incoming', { method: 'POST', headers: { 'x-worker-token': tok, 'content-type': 'application/json' }, body: JSON.stringify({ user_id: 'wakeuser', text: 'fiyat nedir, teşekkürler', msg_id: 'm1', is_group: false }) }));\n"
            "const inc1j = await inc1.json();\n"
            "const dup1 = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/mock-incoming', { method: 'POST', headers: { 'x-worker-token': tok, 'content-type': 'application/json' }, body: JSON.stringify({ user_id: 'wakeuser', text: 'fiyat nedir, teşekkürler', msg_id: 'm1', is_group: false }) }));\n"
            "const dup1j = await dup1.json();\n"
            "const grp2 = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/mock-incoming', { method: 'POST', headers: { 'x-worker-token': tok, 'content-type': 'application/json' }, body: JSON.stringify({ user_id: 'wakeuser', text: 'fiyat nedir', msg_id: 'm2', is_group: true }) }));\n"
            "const grp2j = await grp2.json();\n"
            "const stEnd = await bridge.fetch(new Request('https://aslansite.vercel.app/api/wa_bridge/wa/status?user_id=wakeuser', { headers: { 'x-worker-token': tok } }));\n"
            "const stEndj = await stEnd.json();\n"
            "console.log(JSON.stringify({ unauthW: unauthW.status, authW: authW.status, authWj, inc1j, dup1j, grp2j, stEndj }));\n"
        )
        w_run = subprocess.run(
            ["node", "--input-type=module", "-e", wake_script],
            cwd=str(ROOT), env=b_env, capture_output=True, text=True,
        )
        w_out = {}
        if w_run.returncode == 0 and w_run.stdout.strip():
            try:
                w_out = json.loads(w_run.stdout.strip().splitlines()[-1])
            except Exception:
                w_out = {}
        inc1j = w_out.get("inc1j") or {}
        dup1j = w_out.get("dup1j") or {}
        grp2j = w_out.get("grp2j") or {}
        st_end = w_out.get("stEndj") or {}
        check("Wake-on-message processes rules/emoji, sleeps, deduplicates & enforces group filter",
              w_run.returncode == 0
              and w_out.get("unauthW") == 401
              and w_out.get("authW") == 200 and (w_out.get("authWj") or {}).get("sleep_wake") is True
              and inc1j.get("woken") is True and inc1j.get("replied") is True
              and inc1j.get("replyText") == "Güncel fiyat 500 TL"
              and inc1j.get("emoji") == "❤️"
              and inc1j.get("sleep_state") == "sleeping"
              and inc1j.get("messages_handled") == 1
              and dup1j.get("skipped") is True and dup1j.get("reason") == "duplicate"
              and grp2j.get("replied") is False
              and st_end.get("sleep_state") == "sleeping"
              and st_end.get("messages_handled") == 1,
              (w_run.stdout + w_run.stderr)[:260])

        unauth_w = client.post("/api/wa/wake", json={})
        auth_w_flask = client.post("/api/wa/wake", json={}, headers=hp_hdrs)
        auth_w_vercel = FakeHandler(
            api_index.handler, "POST", path="/api/index.py?__route=/api/wa/wake",
            body=b"{}", headers=hp_hdrs,
        ).run()
        check("POST /api/wa/wake enforces 401 unauth & succeeds 200 on Flask & Vercel",
              unauth_w.status_code == 401
              and auth_w_flask.status_code == 200 and (auth_w_flask.get_json() or {}).get("awake") is True
              and auth_w_vercel.status == 200 and auth_w_vercel.json().get("awake") is True)
    finally:
        if old_wa_url is None:
            os.environ.pop("WA_BOT_WORKER_URL", None)
        else:
            os.environ["WA_BOT_WORKER_URL"] = old_wa_url
        if old_wa_tok is None:
            os.environ.pop("WA_BOT_WORKER_TOKEN", None)
        else:
            os.environ["WA_BOT_WORKER_TOKEN"] = old_wa_tok
        wa_proc.terminate()
        try:
            wa_proc.wait(timeout=5)
        except Exception:
            wa_proc.kill()

    # ------------------------------------------------------------------
    section("13. 60 SANIYELIK SESSIZ KEEP-ALIVE (/api/ping, VERCEL CRON & WORKER)")
    r_ping = client.get("/api/ping")
    r_ping_head = client.head("/api/ping")
    pj_ping = r_ping.get_json() or {}
    check("Flask GET & HEAD /api/ping → 200 + lightweight payload",
          r_ping.status_code == 200 and pj_ping.get("ok") is True
          and set(pj_ping.keys()) == {"ok", "ts"}
          and "no-store" in (r_ping.headers.get("Cache-Control") or "")
          and r_ping_head.status_code == 200)

    r_pq_u = client.post("/api/auth/register", json={
        "username": "PingQuotaUser", "password": good_pw, "password_confirm": good_pw
    })
    pq_hdrs = {"Authorization": f"Bearer {r_pq_u.get_json()['token']}"}
    for _ in range(5):
        client.get("/api/ping", headers=pq_hdrs)
    r_pq_after = client.get("/api/quota", headers=pq_hdrs).get_json() or {}
    check("/api/ping consumes 0 AI & 0 image quota across repeated pings",
          r_pq_after.get("ai", {}).get("remaining") == 5
          and r_pq_after.get("image", {}).get("monthly_remaining") == 2)

    v_ping_get = FakeHandler(api_index.handler, "GET", path="/api/index.py?__route=/api/ping").run()
    v_ping_head = FakeHandler(api_index.handler, "HEAD", path="/api/index.py?__route=/api/ping").run()
    v_ping_hdrs = {k.lower(): v[-1] for k, v in v_ping_get.header_map().items()}
    check("Vercel GET & HEAD /api/ping → 200 + no-store",
          v_ping_get.status == 200 and v_ping_get.json().get("ok") is True
          and "no-store" in v_ping_hdrs.get("cache-control", "")
          and v_ping_head.status == 200)

    r_ka_get = client.get("/api/whatsapp/keep-alive", headers=pq_hdrs)
    r_ka_head = client.head("/api/whatsapp/keep-alive")
    r_ka_post = client.post("/api/whatsapp/keep-alive")
    v_ka_get = FakeHandler(api_index.handler, "GET", path="/api/index.py?__route=/api/whatsapp/keep-alive").run()
    v_ka_head = FakeHandler(api_index.handler, "HEAD", path="/api/index.py?__route=/api/whatsapp/keep-alive").run()
    v_ka_post = FakeHandler(api_index.handler, "POST", path="/api/index.py?__route=/api/whatsapp/keep-alive", body=b"{}").run()
    v_ka_hdrs = {k.lower(): v[-1] for k, v in v_ka_get.header_map().items()}
    r_pq_ka_after = client.get("/api/quota", headers=pq_hdrs).get_json() or {}
    check("Flask & Vercel GET/HEAD/POST /api/whatsapp/keep-alive → 200 + no-store + 0 quota",
          r_ka_get.status_code == 200 and (r_ka_get.get_json() or {}).get("ok") is True
          and r_ka_head.status_code == 200
          and r_ka_post.status_code == 200 and (r_ka_post.get_json() or {}).get("ok") is True
          and "no-store" in (r_ka_get.headers.get("Cache-Control") or "")
          and v_ka_get.status == 200 and v_ka_get.json().get("ok") is True
          and v_ka_head.status == 200
          and v_ka_post.status == 200 and v_ka_post.json().get("ok") is True
          and "no-store" in v_ka_hdrs.get("cache-control", "")
          and r_pq_ka_after.get("ai", {}).get("remaining") == 5
          and r_pq_ka_after.get("image", {}).get("monthly_remaining") == 2)

    bridge_ka_src = (ROOT / "api" / "wa_bridge.mjs").read_text(encoding="utf-8")
    check("api/wa_bridge.mjs implements lightweightPreFilterMessage, wakeAndProcessMessage & 60s self-wake cap",
          "function lightweightPreFilterMessage" in bridge_ka_src
          and "async function wakeAndProcessMessage" in bridge_ka_src
          and "WA_DEEP_SLEEP_INTERVAL_MS || 60000" in bridge_ka_src)

    check("vercel.json configures cron schedule on /api/ping",
          any(c.get("path") == "/api/ping" and c.get("schedule") in ("* * * * *", "0 0 * * *")
              for c in vj.get("crons", [])))

    wa_srv_src = (wa_dir / "server.mjs").read_text(encoding="utf-8")
    wa_rt_src = (wa_dir / "bot_runtime.mjs").read_text(encoding="utf-8")
    mw_src = (ROOT / "worker" / "media_worker.py").read_text(encoding="utf-8")
    check("Node WA & Python Media workers implement 60s silent keep-alive & /ping",
          "KEEPALIVE_INTERVAL_MS" in wa_srv_src and "keepAliveActiveBots" in wa_rt_src
          and "restorePersistedBots" in wa_rt_src
          and "https://aslansite.vercel.app/api/ping" in wa_srv_src
          and "ipv4Get" in ipv4_src and "/ping" in mw_src and "_start_keepalive_loop" in mw_src)

    html_low = html_txt.lower()
    check("Frontend has silent 60s /api/ping timer with zero console logs or UI text",
          "/api/ping" in js_txt and "60000" in js_txt and "visibilitychange" in js_txt
          and "console.log" not in js_txt
          and all(t not in html_low for t in ("keep-alive", "/api/ping", "uyanık tut", "uyanik tut")))

    # ------------------------------------------------------------------
    section("SUMMARY")
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [r for r in results if not r[1]]
    print(f"  Total: {len(results)}   Passed: {passed}   Failed: {len(failed)}")
    for name, _, detail in failed:
        print(f"    \033[91mFAILED\033[0m: {name}  → {detail}")

    shutil.rmtree(WORK, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
