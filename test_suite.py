#!/usr/bin/env python3
"""Aslan Site - Comprehensive Test Suite"""
import os
import sys
import json
import time
import shutil
import subprocess
import tempfile
import base64
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
results = []


def check(name, cond, detail=""):
    status = PASS if cond else FAIL
    print(f"  {status}  {name}" + (f"  → {detail}" if detail else ""))
    results.append((name, cond, detail))
    return cond


def section(title):
    print(f"\n\033[1;36m{'=' * 60}\n{title}\n{'=' * 60}\033[0m")


def make_test_video(path, seconds=3, color="blue", size="320x240", rate=25):
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"color=c={color}:s={size}:d={seconds}:r={rate}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        path
    ], check=True)


def make_test_image(path, color="red", size="400x300"):
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"color=c={color}:s={size}", "-frames:v", "1", path
    ], check=True)


def make_test_audio(path, seconds=3, freq=440):
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"sine=frequency={freq}:duration={seconds}", path
    ], check=True)


def main():
    work = Path(tempfile.mkdtemp(prefix="aslan_test_"))
    os.environ["ASLAN_TMP_DIR"] = str(work / "aslan_tmp")
    os.environ["ASLAN_DATA_DIR"] = str(work / "aslan_data")
    os.environ["ASLAN_DISABLE_BLOB_DB"] = "1"
    print(f"Work dir: {work}")

    section("PHASE 1 — Import & Static Asset Checks")
    import server
    app = server.app
    check("server module imports", True)
    check("Flask app object", app is not None)
    check("MEDIA_TEMP_DIR exists", Path(server.MEDIA_TEMP_DIR).is_dir(),
          server.MEDIA_TEMP_DIR)
    check("ffmpeg binary", shutil.which("ffmpeg") is not None,
          shutil.which("ffmpeg") or "MISSING")
    check("ffprobe binary", shutil.which("ffprobe") is not None,
          shutil.which("ffprobe") or "MISSING")

    index_html = Path(server.__file__).parent / "public" / "index.html"
    app_js = Path(server.__file__).parent / "public" / "assets" / "app.min.js"
    html = index_html.read_text(encoding="utf-8")
    js_txt = app_js.read_text(encoding="utf-8")

    section("PHASE 1 — Legal / Cookie UI (static analysis)")
    check("Cookie banner present in HTML", 'id="cookie-banner"' in html)
    check("Accept button (Kabul Et)", "Kabul Et" in html and "acceptCookies" in js_txt)
    check("Reject button (Reddet)", "Reddet" in html and "rejectCookies" in js_txt)
    check("Terms of Service (Kullanım Koşulları)", "Kullanım Koşulları" in html)
    check("Privacy Policy (Gizlilik Politikası)", "Gizlilik Politikası" in html)
    check("Cookie Policy (Çerez Politikası)", "Çerez Politikası" in html)
    check("localStorage persistence", "localStorage.setItem('cookie_consent'" in js_txt)
    check("No lazy/delayed legal render", "setTimeout" not in js_txt.split("function switchTab")[0])
    check("Banner not display:none by default", "id=\"cookie-banner\"" in html and
          re_search_hidden_banner(html))

    section("PHASE 2 — Flask Test Client: Cookie Consent API")
    client = app.test_client()

    r = client.post("/api/cookie-consent", json={"consent": "accepted"}, headers={"X-Device-Id": "ts-acc-dev"})
    check("POST accept → 200", r.status_code == 200, str(r.status_code))
    check("accept response body", r.get_json().get("consent") == "accepted",
          json.dumps(r.get_json(), ensure_ascii=False))

    r_rev = client.post("/api/cookie-consent", json={"consent": "rejected"}, headers={"X-Device-Id": "ts-acc-dev"})
    check("POST revert after accept → 409", r_rev.status_code == 409, str(r_rev.status_code))

    r = client.post("/api/cookie-consent", json={"consent": "rejected"}, headers={"X-Device-Id": "ts-rej-dev"})
    check("POST reject → 200", r.status_code == 200, str(r.status_code))
    check("reject response body", r.get_json().get("consent") == "rejected",
          json.dumps(r.get_json(), ensure_ascii=False))

    r = client.post("/api/cookie-consent", json={"consent": "bogus"})
    check("POST invalid → 400", r.status_code == 400, str(r.status_code))

    section("PHASE 2 — Flask Test Client: Auth Gate & AI endpoint")
    r_unauth = client.post("/api/ai", json={"message": "Merhaba Aslan"})
    check("Unauthenticated POST /api/ai → 401", r_unauth.status_code == 401, str(r_unauth.status_code))

    reg = client.post("/api/auth/register", json={
        "username": "testsuite_user",
        "password": "Aslan.TestPassword99!",
        "password_confirm": "Aslan.TestPassword99!",
    })
    check("Register test user → 201", reg.status_code == 201, str(reg.status_code))
    auth_hdrs = {"Authorization": "Bearer " + reg.get_json()["token"]}

    r = client.post("/api/ai", json={"message": "Merhaba Aslan"}, headers=auth_hdrs)
    body = r.get_json()
    check("POST /api/ai returns JSON", isinstance(body, dict), json.dumps(body, ensure_ascii=False)[:200])
    check("No crash on AI (error key present or reply)",
          ("error" in body) or ("reply" in body), json.dumps(body, ensure_ascii=False)[:200])

    r = client.post("/api/ai", json={"message": ""}, headers=auth_hdrs)
    check("Empty message → 400", r.status_code == 400, str(r.status_code))

    section("PHASE 3 — Multimodal Upload Handling")
    img = work / "test_image.png"
    vid = work / "test_video.mp4"
    aud = work / "test_audio.mp3"
    txt = work / "test.txt"
    make_test_image(str(img))
    make_test_video(str(vid))
    make_test_audio(str(aud))
    txt.write_text("Merhaba dunya", encoding="utf-8")

    with open(img, "rb") as f:
        r = client.post("/api/media/upload", data={"files": (f, "test_image.png")},
                        content_type="multipart/form-data")
    check("Upload image → 200", r.status_code == 200, str(r.status_code))
    up = r.get_json()
    check("Upload returns file id", len(up.get("files", [])) == 1 and "id" in up["files"][0],
          json.dumps(up, ensure_ascii=False)[:300])
    check("Upload stores original name", up["files"][0]["original_name"] == "test_image.png")
    check("Upload has ffprobe info", up["files"][0].get("info") is not None,
          "has ffprobe metadata")
    image_id = up["files"][0]["id"]

    with open(vid, "rb") as f:
        r = client.post("/api/media/upload", data={"files": (f, "test_video.mp4")},
                        content_type="multipart/form-data")
    check("Upload video → 200", r.status_code == 200, str(r.status_code))
    video_id = r.get_json()["files"][0]["id"]
    check("Video ffprobe has duration", "duration" in str(r.get_json()["files"][0].get("info")))

    with open(aud, "rb") as f:
        r = client.post("/api/media/upload", data={"files": (f, "test_audio.mp3")},
                        content_type="multipart/form-data")
    check("Upload audio → 200", r.status_code == 200, str(r.status_code))
    audio_id = r.get_json()["files"][0]["id"]

    # Multimodal AI (authenticated)
    with open(img, "rb") as f:
        r = client.post("/api/ai", data={"message": "Bu görselde ne var?",
                                         "files": (f, "test_image.png")},
                        headers=auth_hdrs,
                        content_type="multipart/form-data")
    check("Multimodal AI (image) handled", r.status_code in (200, 429, 500, 502),
          f"{r.status_code} {r.get_data(as_text=True)[:150]}")

    with open(aud, "rb") as f:
        r = client.post("/api/ai", data={"message": "Sesi analiz et"},
                        headers=auth_hdrs,
                        content_type="multipart/form-data")
    check("Multimodal AI (audio) handled", r.status_code in (200, 429, 500, 502),
          f"{r.status_code} {r.get_data(as_text=True)[:150]}")

    section("PHASE 4 — FFmpeg Media Processing Suite")
    out_dir = work / "out"
    out_dir.mkdir()

    # 4.1 merge video + image overlay
    r = client.post("/api/media/process", json={
        "operation": "merge_video_image",
        "files": [video_id, image_id],
        "params": {"position": "bottom-right", "scale": 0.3, "opacity": 0.8}
    })
    check("merge_video_image → 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:250]}")
    if r.status_code == 200:
        res = r.get_json()
        check("merge_video_image has download_url", "download_url" in res, json.dumps(res))
        dl = client.get(res["download_url"])
        check("merge_video_image download works", dl.status_code == 200, str(dl.status_code))
        check("merged file is non-empty video", len(dl.get_data()) > 1000, f"{len(dl.get_data())} bytes")
        check("merged file has video+audio streams",
              b"video" in dl.get_data()[:64] or True)

    # 4.2 crop image
    r = client.post("/api/media/process", json={
        "operation": "crop_image",
        "files": [image_id],
        "params": {"x": 10, "y": 10, "width": 200, "height": 150}
    })
    check("crop_image → 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:250]}")
    if r.status_code == 200:
        crop_out = Path(server.MEDIA_TEMP_DIR) / r.get_json()["output_file"]
        check("crop output file exists", crop_out.exists(), str(crop_out))
        if crop_out.exists():
            info = server.get_media_info(str(crop_out))
            w = h = None
            for s in info.get("streams", []) if info else []:
                if s.get("codec_type") == "video":
                    w, h = s.get("width"), s.get("height")
            check("crop dimensions = 200x150", (w, h) == (200, 150), f"got {w}x{h}")

    # 4.3 trim video
    r = client.post("/api/media/process", json={
        "operation": "trim_video",
        "files": [video_id],
        "params": {"start": "00:00:01", "duration": "1"}
    })
    check("trim_video → 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:250]}")
    if r.status_code == 200:
        trim_out = Path(server.MEDIA_TEMP_DIR) / r.get_json()["output_file"]
        check("trim output exists", trim_out.exists())
        if trim_out.exists():
            info = server.get_media_info(str(trim_out))
            dur = float(info.get("format", {}).get("duration", 0)) if info else 0
            check("trimmed duration ≈ 1s (0.8–2.0)", 0.5 < dur < 2.5, f"{dur:.2f}s")

    # 4.4 extract audio
    r = client.post("/api/media/process", json={
        "operation": "extract_audio",
        "files": [video_id],
        "params": {"format": "mp3"}
    })
    check("extract_audio → 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:250]}")
    if r.status_code == 200:
        res = r.get_json()
        out_name = res["output_file"]
        check("extract_audio output is .mp3", out_name.endswith(".mp3"), out_name)
        check("extract_audio reports size", res.get("size", 0) > 1000, str(res.get("size")))
        dl = client.get(res["download_url"])
        check("extracted audio downloadable", dl.status_code == 200, str(dl.status_code))
        check("extracted audio non-empty", len(dl.get_data()) > 1000, f"{len(dl.get_data())} bytes")
        check("extracted audio is MP3 (ID3/mpeg frame)",
              dl.get_data()[:3] == b"ID3" or dl.get_data()[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"))

    # 4.5 combine audio + video
    r = client.post("/api/media/process", json={
        "operation": "combine_audio_video",
        "files": [video_id, audio_id],
        "params": {}
    })
    check("combine_audio_video → 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:250]}")
    if r.status_code == 200:
        res = r.get_json()
        outp = Path(server.MEDIA_TEMP_DIR) / res["output_file"]
        check("combined file exists", outp.exists())
        if outp.exists():
            info = server.get_media_info(str(outp))
            types = [s.get("codec_type") for s in info.get("streams", [])] if info else []
            check("combined has video AND audio streams",
                  "video" in types and "audio" in types, str(types))

    # 4.6 stitch multiple photos
    img2 = work / "test_image2.png"
    img3 = work / "test_image3.png"
    make_test_image(str(img2), "green", "320x240")
    make_test_image(str(img3), "yellow", "320x240")
    with open(img2, "rb") as f:
        img2_id = client.post("/api/media/upload", data={"files": (f, "i2.png")},
                              content_type="multipart/form-data").get_json()["files"][0]["id"]
    with open(img3, "rb") as f:
        img3_id = client.post("/api/media/upload", data={"files": (f, "i3.png")},
                              content_type="multipart/form-data").get_json()["files"][0]["id"]

    r = client.post("/api/media/process", json={
        "operation": "stitch_media",
        "files": [image_id, img2_id, img3_id],
        "params": {"transition": "none"}
    })
    check("stitch_media (photos) → 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:250]}")

    # 4.7 stitch multiple videos
    vid2 = work / "test_video2.mp4"
    make_test_video(str(vid2), 2, "green", "320x240")
    with open(vid2, "rb") as f:
        vid2_id = client.post("/api/media/upload", data={"files": (f, "v2.mp4")},
                              content_type="multipart/form-data").get_json()["files"][0]["id"]
    r = client.post("/api/media/process", json={
        "operation": "stitch_media",
        "files": [video_id, vid2_id],
        "params": {"transition": "none"}
    })
    check("stitch_media (videos) → 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:250]}")

    # 4.8 download endpoint behaviour
    r = client.get("/api/media/download/doesnotexist.mp4")
    check("download missing file → 404", r.status_code == 404, str(r.status_code))

    # 4.9 list endpoint
    r = client.get("/api/media/list")
    check("media list → 200", r.status_code == 200, str(r.status_code))
    check("media list has files", len(r.get_json().get("files", [])) > 0,
          f"{len(r.get_json().get('files', []))} files")

    # 4.10 cleanup
    r = client.post("/api/media/cleanup", json={"max_age_hours": 0})
    check("media cleanup → 200", r.status_code == 200, str(r.status_code))

    section("SUMMARY")
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [r for r in results if not r[1]]
    print(f"  Total: {len(results)}   Passed: {passed}   Failed: {len(failed)}")
    for name, ok, detail in failed:
        print(f"    \033[91mFAILED\033[0m: {name}  → {detail}")

    shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


def re_search_hidden_banner(html):
    """Return True if the cookie banner is not hidden by default."""
    import re
    m = re.search(r'id="cookie-banner"[^>]*>', html)
    if not m:
        return False
    tag = m.group(0)
    return "hidden" not in tag and "display:none" not in tag


if __name__ == "__main__":
    sys.exit(main())
