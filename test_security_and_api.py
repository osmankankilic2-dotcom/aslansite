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
    for secret_path in ("/.env.local", "/.git/config", "/server.py", "/core/config.py", "/requirements.txt"):
        r = client.get(secret_path)
        check(f"Blocked static {secret_path} → 404", r.status_code == 404, str(r.status_code))

    for pub_path in (
        "/", "/manifest.json", "/robots.txt",
        "/assets/logo-mark.png", "/assets/favicon.png",
        "/assets/icon-192.png", "/assets/logo-banner.png",
    ):
        r = client.get(pub_path)
        check(f"Public asset {pub_path} → 200", r.status_code == 200, str(r.status_code))
        if pub_path.endswith(".png"):
            check(f"  {pub_path} is image/png", "image/png" in (r.headers.get("Content-Type") or ""))

    html_txt = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    banner_block = html_txt.split('id="cookie-banner"')[1].split("</div>\n\n<!-- ")[0]
    check("Cookie banner has 'Çerezleri Kabul Et'", "Çerezleri Kabul Et" in banner_block)
    check("Cookie banner has 'Çerezleri Yönet'", "Çerezleri Yönet" in banner_block)
    check("Cookie banner does NOT show reject button directly", "Çerezleri Reddet" not in banner_block)
    modal_block = html_txt.split('id="cookie-modal"')[1].split('id="auth-modal"')[0]
    check("Cookie detail modal has 'Çerezleri Reddet' at bottom", "Çerezleri Reddet" in modal_block)
    check("Auth lock overlays present for TTS and AI",
          'id="tts-auth-lock"' in html_txt and 'id="ai-auth-lock"' in html_txt)

    # ------------------------------------------------------------------
    section("2. HTTP GUVENLIK BASLIKLARI & KONTROLLU CORS")
    r_home = client.get("/")
    check("HTML has Content-Security-Policy", "Content-Security-Policy" in r_home.headers)
    check("HTML has X-Frame-Options: DENY", r_home.headers.get("X-Frame-Options") == "DENY")
    check("HTML has X-Content-Type-Options: nosniff", r_home.headers.get("X-Content-Type-Options") == "nosniff")
    check("HTML has Referrer-Policy", "Referrer-Policy" in r_home.headers)
    check("HTML has Permissions-Policy", "Permissions-Policy" in r_home.headers)

    r_api = client.get("/api/health", headers={"Origin": "https://aslansite.vercel.app"})
    check("GET /api/health → 200", r_api.status_code == 200 and r_api.get_json()["capabilities"]["auth"])
    check("API has Cache-Control: no-store", "no-store" in (r_api.headers.get("Cache-Control") or ""))
    check("Allowed Origin echoed in ACAO",
          r_api.headers.get("Access-Control-Allow-Origin") == "https://aslansite.vercel.app")

    r_evil_post = client.post("/api/quota", json={}, headers={"Origin": "https://evil.example"})
    check("Disallowed Origin POST → 403", r_evil_post.status_code == 403, str(r_evil_post.status_code))

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
    section("7. VERCEL SERVERLESS HANDLER'LAR (api/*.py)")
    from api import ai as api_ai, auth as api_auth, consent as api_consent
    from api import health as api_health, image as api_image, media as api_media
    from api import quota as api_quota, social as api_social, tts as api_tts

    h_res = FakeHandler(api_health.handler, "GET").run()
    check("api/health.py → 200", h_res.status == 200 and h_res.json().get("capabilities", {}).get("free_ai"))

    # Vercel auth handler: register -> login -> me -> AI -> TTS
    v_reg = FakeHandler(
        api_auth.handler, "POST", path="/api/auth/register",
        body=json.dumps({"username": "VercelUser", "password": good_pw, "password_confirm": good_pw}).encode("utf-8"),
    ).run()
    check("api/auth.py /register → 201", v_reg.status == 201 and v_reg.json().get("token"))
    v_tok = v_reg.json()["token"]

    v_ai_unauth = FakeHandler(api_ai.handler, "POST", body=b'{"message":"hi"}').run()
    v_tts_unauth = FakeHandler(api_tts.handler, "POST", body=b'{"text":"hi"}').run()
    check("api/ai.py & api/tts.py unauthenticated → 401", v_ai_unauth.status == 401 and v_tts_unauth.status == 401)

    with mock.patch.object(ai_engine, "generate_reply", return_value=("Vercel AI yaniti", None)):
        v_ai_ok = FakeHandler(
            api_ai.handler, "POST",
            body=json.dumps({"message": "Merhaba"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {v_tok}"},
        ).run()
    check("api/ai.py authenticated → 200 + DB quota=4",
          v_ai_ok.status == 200 and v_ai_ok.json()["quota"]["ai"]["remaining"] == 4)

    v_q = FakeHandler(api_quota.handler, "GET", headers={"Authorization": f"Bearer {v_tok}"}).run()
    check("api/quota.py returns account-bound quota=4",
          v_q.status == 200 and v_q.json().get("account_bound") and v_q.json()["ai"]["remaining"] == 4)

    # api/media.py crop + cleanup
    before_files = set(os.listdir(cfg.MEDIA_TEMP_DIR))
    img_b64 = base64.b64encode(Path(imgs[0]).read_bytes()).decode("ascii")
    m_res = FakeHandler(api_media.handler, "POST", body=json.dumps({
        "operation": "crop_image",
        "files": [{"name": "a.png", "mime_type": "image/png", "data": img_b64}],
        "params": {"x": 0, "y": 0, "width": 100, "height": 80},
    }).encode("utf-8")).run()
    after_files = set(os.listdir(cfg.MEDIA_TEMP_DIR))
    check("api/media.py crop_image → 200 & /tmp clean", m_res.status == 200 and before_files == after_files)

    v_soc = FakeHandler(api_social.handler, "POST", body=b'{"url":"http://127.0.0.1/?youtube.com"}').run()
    check("api/social.py SSRF → 400", v_soc.status == 400)

    v_con = FakeHandler(api_consent.handler, "POST", body=json.dumps({
        "consent": "custom", "preferences": {"functional": True, "analytics": False}
    }).encode("utf-8")).run()
    check("api/consent.py custom prefs → 200", v_con.status == 200 and v_con.json().get("consent") == "custom")

    with mock.patch.object(ai_engine, "generate_image", return_value=(False, {"quota_exceeded": False, "error": "err"})):
        v_img_err = FakeHandler(
            api_image.handler, "POST",
            body=json.dumps({"prompt": "kedi"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {v_tok}"},
        ).run()
    check("api/image.py upstream error → 502", v_img_err.status == 502)

    # ------------------------------------------------------------------
    section("8. SOZDIZIMI KONTROLLERI (PYTHON + JS + VERCEL.JSON)")
    py_files = [str(ROOT / "server.py")] + [str(p) for p in (ROOT / "core").glob("*.py")] + [str(p) for p in (ROOT / "api").glob("*.py")]
    cp = subprocess.run(["python3", "-m", "py_compile", *py_files], capture_output=True, text=True)
    check("All Python files py_compile OK", cp.returncode == 0, cp.stderr[:200])

    scripts = re.findall(r"<script>(.*?)</script>", html_txt, re.DOTALL)
    js_path = WORK / "check.js"
    js_path.write_text("\n".join(scripts), encoding="utf-8")
    nc = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
    check("public/index.html JS syntax valid", nc.returncode == 0, nc.stderr[:200])

    vj = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    check("vercel.json has /api/auth/:path* & <= 12 functions",
          any(r.get("destination") == "/api/auth.py" for r in vj.get("rewrites", []))
          and len(list((ROOT / "api").glob("*.py"))) <= 12)

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
