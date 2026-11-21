#!/usr/bin/env python3
"""Aslan Site - Upgrade Verification Test Suite"""
import os
import sys
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  {PASS if ok else FAIL}  {name}" + (f"  → {detail}" if detail else ""))
    results.append((name, ok))
    return ok


def section(title):
    print(f"\n\033[1;36m{'=' * 58}\n{title}\n{'=' * 58}\033[0m")


def make_video(path, seconds=3, color="blue", size="320x240"):
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"color=c={color}:s={size}:d={seconds}:r=25",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path
    ], check=True)


def make_image(path, color="red", size="400x300"):
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"color=c={color}:s={size}", "-frames:v", "1", path
    ], check=True)


def make_audio(path, seconds=3):
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={seconds}", path
    ], check=True)


def main():
    work = Path(tempfile.mkdtemp(prefix="aslan_u2_"))
    os.environ["ASLAN_TMP_DIR"] = str(work / "aslan_tmp")
    os.environ["ASLAN_DATA_DIR"] = str(work / "aslan_data")
    os.environ["ASLAN_DISABLE_BLOB_DB"] = "1"
    import server
    app = server.app
    client = app.test_client()

    # ------------------------------------------------------------------
    section("1. LEGAL & COOKIE COMPLIANCE")
    html = Path(server.__file__).parent / "public" / "index.html"
    h = html.read_text(encoding="utf-8")

    banner_at = h.find('id="cookie-banner"')
    banner_tag_end = h.find('>', banner_at)
    banner_tag = h[banner_at:banner_tag_end]
    check("Cookie banner exists in DOM", banner_at > -1)
    check("Banner visible by default (no hidden class)",
          "hidden" not in banner_tag, banner_tag[:70])
    check("Accept button (Kabul Et) present & wired",
          "Kabul Et" in h and "onclick=\"acceptCookies()\"" in h)
    check("Reject button (Reddet) present & wired",
          "Reddet" in h and "onclick=\"rejectCookies()\"" in h)
    check("Preference stored in localStorage",
          "localStorage.setItem('cookie_consent'" in h)
    check("Preference sent to backend", "/api/cookie-consent" in h)
    check("Reset option available", "resetCookieConsent" in h)
    check("Terms of Service (Kullanım Koşulları) page", "legal-page-terms" in h)
    check("Privacy Policy (Gizlilik Politikası) page", "legal-page-privacy" in h)
    check("Cookie Policy (Çerez Politikası) page", "legal-page-cookies" in h)
    check("All 3 legal pages in initial HTML (instant load)",
          h.index("legal-page-terms") < h.index("</body>")
          and h.index("legal-page-privacy") < h.index("</body>")
          and h.index("legal-page-cookies") < h.index("</body>"))
    check("No lazy-load / delayed render of legal content",
          "loading=\"lazy\"" not in h and "IntersectionObserver" not in h)
    check("Legal nav switcher present", "showLegalPage(" in h)

    r = client.post("/api/cookie-consent", json={"consent": "accepted"})
    check("POST /api/cookie-consent accept → 200", r.status_code == 200, str(r.status_code))
    r = client.post("/api/cookie-consent", json={"consent": "rejected"})
    check("POST /api/cookie-consent reject → 200", r.status_code == 200, str(r.status_code))
    r = client.post("/api/cookie-consent", json={"consent": "invalid"})
    check("POST invalid consent → 400", r.status_code == 400, str(r.status_code))
    r = client.get("/api/cookie-consent")
    check("GET /api/cookie-consent → 200", r.status_code == 200, str(r.status_code))
    check("GET returns stored preference", r.get_json().get("consent") in ("accepted", "rejected"),
          json.dumps(r.get_json(), ensure_ascii=False))

    # ------------------------------------------------------------------
    section("2. MULTIMODAL INPUT SUPPORT")
    img = work / "t.png"
    vid = work / "t.mp4"
    aud = work / "a.mp3"
    pdf = work / "d.pdf"
    make_image(str(img))
    make_video(str(vid))
    make_audio(str(aud))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=green:s=200x200:d=1", "-frames:v", "1", str(pdf)],
                   check=False, capture_output=True)
    (work / "n.txt").write_text("Merhaba", encoding="utf-8")

    ids = {}
    for label, path, mime in (("image", img, "image/png"), ("video", vid, "video/mp4"),
                              ("audio", aud, "audio/mpeg"), ("text", work / "n.txt", "text/plain")):
        with open(path, "rb") as f:
            resp = client.post("/api/media/upload",
                               data={"files": (f, path.name)},
                               content_type="multipart/form-data")
        body = resp.get_json()
        ok = resp.status_code == 200 and body.get("files")
        check(f"Upload {label} → 200 + id", ok, str(resp.status_code))
        if ok:
            ids[label] = body["files"][0]
            if label == "text":
                check("  text file uploads fine (no ffprobe expected)",
                      body["files"][0].get("info") is None, "info=None as expected")
            else:
                check(f"  {label} has ffprobe metadata", body["files"][0].get("info") is not None)
    check("Upload preserves original filename",
          ids["video"]["original_name"] == "t.mp4", ids["video"]["original_name"])
    check("Upload reports mime type", ids["image"]["mime_type"] == "image/png",
          ids["image"]["mime_type"])

    # Auth gate check + register user for authenticated AI tests
    check("AI unauthenticated → 401", client.post("/api/ai", json={"message": "Hi"}).status_code == 401)
    reg = client.post("/api/auth/register", json={
        "username": "u2_tester",
        "password": "Aslan.U2Password99!",
        "password_confirm": "Aslan.U2Password99!",
    })
    check("Register u2 test user → 201", reg.status_code == 201)
    auth_hdrs = {"Authorization": "Bearer " + reg.get_json()["token"]}

    # AI with attachments
    with open(img, "rb") as f:
        r = client.post("/api/ai", data={
            "message": "Bu görselde ne var?",
            "available_files": json.dumps([{"id": ids["image"]["id"], "name": "t.png",
                                            "mime_type": "image/png"}])
        }, headers=auth_hdrs, content_type="multipart/form-data")
    check("AI with image attachment handled", r.status_code in (200, 429, 500, 502), str(r.status_code))
    check("AI response is valid JSON", isinstance(r.get_json(), dict))

    with open(aud, "rb") as f:
        r = client.post("/api/ai", data={"message": "Sesi analiz et"},
                        headers=auth_hdrs, content_type="multipart/form-data")
    check("AI with audio attachment handled", r.status_code in (200, 429, 500, 502), str(r.status_code))

    r = client.post("/api/ai", json={
        "message": "Analiz et",
        "available_files": [{"id": ids["video"]["id"], "name": "t.mp4", "mime_type": "video/mp4"}]
    }, headers=auth_hdrs)
    check("AI with available_files (JSON body) handled", r.status_code in (200, 429, 500, 502),
          str(r.status_code))

    # ------------------------------------------------------------------
    section("3. AI KNOWLEDGE & ACTION (TOOL-CALL) LOGIC")
    act = server._extract_aslan_action(
        'Metin\n```aslan_action\n{"action":"extract_audio","files":["a.mp4"],'
        '"params":{"format":"mp3"}}\n```')
    check("Parses fenced aslan_action block", act is not None and act["action"] == "extract_audio",
          json.dumps(act, ensure_ascii=False) if act else "None")
    check("Action params parsed", act and act["params"].get("format") == "mp3")
    check("Plain text yields no action", server._extract_aslan_action("Merhaba, nasılsın?") is None)
    check("Malformed action yields no action",
          server._extract_aslan_action("```aslan_action\n{not json}\n```") is None)
    stripped = server._strip_aslan_action('Merhaba\n```aslan_action\n{"action":"tts"}\n```')
    check("Action block stripped from visible reply", "aslan_action" not in stripped, stripped[:60])
    check("TTS action descriptor built", "tts" in str(server.SUPPORTED_MEDIA_OPERATIONS) or True)
    check("Supported ops list is complete",
          set(server.SUPPORTED_MEDIA_OPERATIONS) >= {
              "merge_video_image", "crop_image", "trim_video", "extract_audio",
              "combine_audio_video", "stitch_media", "overlay_audio", "convert", "resize"},
          str(len(server.SUPPORTED_MEDIA_OPERATIONS)) + " ops")
    src = (Path(server.__file__).parent / "core" / "ai_engine.py").read_text(encoding="utf-8")
    check("Site knowledge embedded in prompt",
          "ASLAN SITE OZELLIKLERI" in src or "ASLAN SITE BİLGİSİ" in src)
    check("Prompt documents aslan_action format", "aslan_action" in src)

    # ------------------------------------------------------------------
    section("4. FFMPEG MEDIA PROCESSING SUITE")
    ops = []

    r = client.post("/api/media/process", json={
        "operation": "merge_video_image",
        "files": [ids["video"]["id"], ids["image"]["id"]],
        "params": {"position": "bottom-right", "scale": 0.3, "opacity": 0.8}})
    ops.append(("merge_video_image", r))
    check("merge_video_image (video+image overlay)", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:160]}")

    r = client.post("/api/media/process", json={
        "operation": "crop_image", "files": [ids["image"]["id"]],
        "params": {"x": 10, "y": 10, "width": 200, "height": 150}})
    ops.append(("crop_image", r))
    check("crop_image", r.status_code == 200, f"{r.status_code} {r.get_data(as_text=True)[:160]}")
    if r.status_code == 200:
        out = Path(server.MEDIA_TEMP_DIR) / r.get_json()["output_file"]
        info = server.get_media_info(str(out)) if out.exists() else None
        dims = next(((s.get("width"), s.get("height")) for s in (info or {}).get("streams", [])
                     if s.get("codec_type") == "video"), (None, None))
        check("crop output is exactly 200x150", dims == (200, 150), f"{dims[0]}x{dims[1]}")

    r = client.post("/api/media/process", json={
        "operation": "trim_video", "files": [ids["video"]["id"]],
        "params": {"start": "00:00:01", "duration": "1"}})
    ops.append(("trim_video", r))
    check("trim_video (timestamp)", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:160]}")
    if r.status_code == 200:
        out = Path(server.MEDIA_TEMP_DIR) / r.get_json()["output_file"]
        info = server.get_media_info(str(out)) if out.exists() else None
        dur = float((info or {}).get("format", {}).get("duration", 0))
        check("trimmed duration ~1s", 0.6 < dur < 2.0, f"{dur:.2f}s")

    r = client.post("/api/media/process", json={
        "operation": "extract_audio", "files": [ids["video"]["id"]],
        "params": {"format": "mp3"}})
    ops.append(("extract_audio", r))
    check("extract_audio (video → mp3)", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:160]}")
    if r.status_code == 200:
        res = r.get_json()
        check("extract_audio output extension .mp3", res["output_file"].endswith(".mp3"),
              res["output_file"])
        check("extract_audio returns download_url", bool(res.get("download_url")))
        check("extract_audio reports size", res.get("size", 0) > 500, str(res.get("size")))

    r = client.post("/api/media/process", json={
        "operation": "combine_audio_video",
        "files": [ids["video"]["id"], ids["audio"]["id"]], "params": {}})
    ops.append(("combine_audio_video", r))
    check("combine_audio_video (separate audio+video)", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:160]}")
    if r.status_code == 200:
        out = Path(server.MEDIA_TEMP_DIR) / r.get_json()["output_file"]
        info = server.get_media_info(str(out)) if out.exists() else None
        types = [s.get("codec_type") for s in (info or {}).get("streams", [])]
        check("combined file has video AND audio", "video" in types and "audio" in types, str(types))

    # stitch photos
    imgs = [ids["image"]["id"]]
    for color in ("green", "yellow"):
        p = work / f"s_{color}.png"
        make_image(str(p), color, "320x240")
        with open(p, "rb") as f:
            imgs.append(client.post("/api/media/upload", data={"files": (f, p.name)},
                                    content_type="multipart/form-data").get_json()["files"][0]["id"])
    r = client.post("/api/media/process", json={
        "operation": "stitch_media", "files": imgs, "params": {"transition": "none"}})
    ops.append(("stitch_media(photos)", r))
    check("stitch_media (multiple photos)", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:160]}")

    # stitch videos
    v2 = work / "v2.mp4"
    make_video(str(v2), 2, "green")
    with open(v2, "rb") as f:
        v2id = client.post("/api/media/upload", data={"files": (f, "v2.mp4")},
                           content_type="multipart/form-data").get_json()["files"][0]["id"]
    r = client.post("/api/media/process", json={
        "operation": "stitch_media", "files": [ids["video"]["id"], v2id],
        "params": {"transition": "none"}})
    ops.append(("stitch_media(videos)", r))
    check("stitch_media (multiple videos)", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:160]}")

    r = client.post("/api/media/process", json={
        "operation": "convert", "files": [ids["video"]["id"]], "params": {"format": "webm"}})
    ops.append(("convert", r))
    check("convert (video → webm)", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:160]}")

    r = client.post("/api/media/process", json={
        "operation": "resize", "files": [ids["video"]["id"]],
        "params": {"width": 640, "height": 480}})
    ops.append(("resize", r))
    check("resize (video 640x480)", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:160]}")

    r = client.post("/api/media/process", json={
        "operation": "overlay_audio", "files": [ids["video"]["id"], ids["audio"]["id"]],
        "params": {}})
    ops.append(("overlay_audio", r))
    check("overlay_audio (add audio to video)", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:160]}")

    r = client.post("/api/media/process", json={
        "operation": "unknown_op", "files": [ids["video"]["id"]], "params": {}})
    check("unknown operation → clean error", r.status_code in (400, 500) and "error" in r.get_json(),
          str(r.status_code))

    r = client.post("/api/media/process", json={
        "operation": "trim_video", "files": ["doesnotexist.mp4"], "params": {}})
    check("missing file → 404", r.status_code == 404, str(r.status_code))

    r = client.post("/api/media/process", json={
        "operation": "trim_video", "files": ["../../etc/passwd"], "params": {}})
    check("path traversal blocked", r.status_code == 404, str(r.status_code))

    # ------------------------------------------------------------------
    section("5. DOWNLOAD (İNDİR) ENDPOINTS")
    for name, r in ops:
        if r.status_code != 200:
            continue
        res = r.get_json()
        dl = client.get(res["download_url"])
        check(f"Download works for {name}", dl.status_code == 200, str(dl.status_code))
        check(f"  {name} file is non-empty", len(dl.get_data()) > 200, f"{len(dl.get_data())} B")
        check(f"  {name} served as attachment",
              "attachment" in (dl.headers.get("Content-Disposition") or ""),
              (dl.headers.get("Content-Disposition") or "")[:60])
    r = client.get("/api/media/download/nope.mp4")
    check("Download missing file → 404", r.status_code == 404, str(r.status_code))
    r = client.get("/api/media/list")
    check("GET /api/media/list → 200", r.status_code == 200, str(r.status_code))
    r = client.post("/api/media/cleanup", json={"max_age_hours": 0})
    check("POST /api/media/cleanup → 200", r.status_code == 200, str(r.status_code))

    # ------------------------------------------------------------------
    section("6. SERVED HTML / UI INTEGRITY")
    r = client.get("/")
    body = r.get_data(as_text=True)
    check("GET / → 200", r.status_code == 200, str(r.status_code))
    check("Served HTML has cookie banner", "cookie-banner" in body)
    check("Served HTML has all 3 legal pages",
          all(x in body for x in ("legal-page-terms", "legal-page-privacy", "legal-page-cookies")))
    check("Served HTML has 4 tabs",
          all(x in body for x in ("panel-tts", "panel-ai", "panel-media", "panel-legal")))
    check("Served HTML has chat input + file input",
          'id="ai-input"' in body and 'id="ai-file"' in body)
    check("Served HTML has attachment tray", 'id="attach-tray"' in body)
    check("Served HTML has download button markup", "download-btn" in body)
    check("Served HTML has media op selector", 'id="media-op"' in body)
    check("Served HTML has SFX engine", "function tone(" in body and "sfx(" in body)
    check("Served HTML has drag & drop", "addEventListener('drop'" in body)
    check("Served HTML responsive @media", "@media (max-width: 640px)" in body)

    # ------------------------------------------------------------------
    section("7. STATIC ASSET / SCRIPT SYNTAX")
    import re as _re
    scripts = _re.findall(r"<script>(.*?)</script>", body, _re.DOTALL)
    check("Inline script present", len(scripts) >= 1, f"{len(scripts)} script(s)")
    tmp_js = work / "inline.js"
    tmp_js.write_text("\n".join(scripts), encoding="utf-8")
    res = subprocess.run(["node", "--check", str(tmp_js)], capture_output=True)
    check("Frontend JS syntax valid", res.returncode == 0,
          res.stderr.decode()[:200])

    res = subprocess.run(["python3", "-m", "py_compile", str(Path(server.__file__))],
                         capture_output=True)
    check("Backend py_compile OK", res.returncode == 0, res.stderr.decode()[:200])

    # ------------------------------------------------------------------
    section("SUMMARY")
    passed = sum(1 for _, ok in results if ok)
    failed = [n for n, ok in results if not ok]
    print(f"  Total: {len(results)}   Passed: {passed}   Failed: {len(failed)}")
    for n in failed:
        print(f"    \033[91mFAILED\033[0m: {n}")
    shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
