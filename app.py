import html as htmllib
import os
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

import requests
import yt_dlp
from flask import Flask, Response, abort, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=None)
BASE = os.path.dirname(os.path.abspath(__file__))

IG_URL = re.compile(r"^https?://(www\.)?instagram\.com/([\w.]+/)?(p|reel|reels|tv)/[\w-]+", re.I)
ALLOWED_CDN = ("cdninstagram.com", "fbcdn.net")
COOKIES = os.environ.get("COOKIES_FILE")  # اختياري: ملف cookies.txt


def allowed_host(url):
    host = urlparse(url).hostname or ""
    return urlparse(url).scheme in ("http", "https") and host.endswith(ALLOWED_CDN)


def pick_username(data):
    """يرجع اسم المستخدم (وليس الاسم الظاهر أو الرقم)."""
    for key in ("channel", "uploader_id", "uploader"):
        val = str(data.get(key) or "").strip()
        if val and not val.isdigit():
            return re.sub(r"[^A-Za-z0-9._]", "", val) or "instagram"
    return "instagram"


def best_format(info, username, index, post_code=None):
    """يرجع أفضل ملف يحتوي صورة وصوتاً معاً (بدون الحاجة إلى دمج)."""
    url = info.get("url")
    ext = info.get("ext") or "mp4"
    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    audio_url = None
    if formats:
        video_only = [f for f in formats
                      if f.get("vcodec") not in (None, "none") and f.get("acodec") == "none"]
        audio_only = [f for f in formats
                      if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
        quality = lambda f: (f.get("height") or 0, f.get("tbr") or 0)
        if video_only and audio_only:
            # الصورة والصوت منفصلان: ندمجهما أثناء التحميل
            pick = max(video_only, key=quality)
            audio_url = max(audio_only, key=lambda f: f.get("abr") or f.get("tbr") or 0)["url"]
        else:
            pool = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") != "none"]
            pool = pool or [f for f in formats if f.get("vcodec") != "none"] or formats
            pick = max(pool, key=quality)
        url, ext = pick["url"], pick.get("ext") or ext
    if not url:
        thumbs = info.get("thumbnails") or []
        biggest = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
                      default={}).get("url")
        url = info.get("thumbnail") or biggest
        ext = os.path.splitext(urlparse(url or "").path)[1].lstrip(".").lower()
        ext = ext if ext in ("jpg", "jpeg", "png", "webp") else "jpg"
    kind = "image" if ext in ("jpg", "jpeg", "png", "webp") else "video"
    code = re.sub(r"[^\w-]", "", str(post_code or info.get("id") or ""))
    filename = f"{username}_{code}_{index}.{ext}" if code else f"{username}_{index}.{ext}"
    return {"url": url, "ext": ext, "type": kind,
            "thumb": info.get("thumbnail"), "filename": filename,
            "audio_url": audio_url}


ANSI = re.compile(r"\x1b\[[0-9;]*m")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def clean_url(u):
    u = u.replace("\\/", "/").replace("\\u0026", "&").replace("\\u0025", "%")
    return htmllib.unescape(u).replace("\\", "")


def parse_embed(page):
    """يستخرج اسم المستخدم وروابط الصور من صفحة التضمين."""
    urls = re.findall(r'display_url\\*"\s*:\s*\\*"(https:.*?)\\*"', page)
    urls += re.findall(r'class="EmbeddedMediaImage"[^>]*?src="([^"]+)"', page)
    seen, images = set(), []
    for u in map(clean_url, urls):
        if allowed_host(u) and u not in seen:
            seen.add(u)
            images.append(u)
    m = (re.search(r'\\*"username\\*"\s*:\s*\\*"([\w.]+)', page)
         or re.search(r'UsernameText">([\w.]+)<', page))
    return (m.group(1) if m else "instagram"), images


def embed_fallback(link):
    """احتياط لمنشورات الصور عندما يفشل yt-dlp."""
    m = re.search(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([\w-]+)", link)
    if not m:
        return "instagram", []
    code = m.group(1)
    try:
        r = requests.get(f"https://www.instagram.com/p/{code}/embed/captioned/",
                         headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            return "instagram", []
        username, images = parse_embed(r.text)
    except Exception as e:
        app.logger.warning("embed fallback failed: %s", e)
        return "instagram", []
    items = [{"url": u, "ext": "jpg", "type": "image", "thumb": u,
              "filename": f"{username}_{code}_{i + 1}.jpg", "audio_url": None}
             for i, u in enumerate(images)]
    return username, items


@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.get("/api/info")
def info():
    link = request.args.get("url", "").strip()
    if not IG_URL.match(link):
        return jsonify(error="الرابط غير صالح. استخدم رابط بوست أو ريل من انستغرام."), 400

    opts = {"quiet": True, "skip_download": True, "noplaylist": False,
            "ignore_no_formats_error": True}
    if COOKIES and os.path.exists(COOKIES):
        opts["cookiefile"] = COOKIES

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(link, download=False)
    except Exception as e:
        app.logger.warning("yt-dlp failed: %s", e)
        username, items = embed_fallback(link)
        if items:
            return jsonify(title=username, items=items)
        detail = ANSI.sub("", str(e))[:300]
        return jsonify(error="تعذر جلب المحتوى. قد يكون الحساب خاصاً أو الرابط خاطئ.",
                       detail=detail), 422

    entries = data.get("entries") or [data]
    username = pick_username(data)
    items = [best_format(e, username, i + 1, data.get("id")) for i, e in enumerate(entries) if e]
    items = [i for i in items if i["url"] and allowed_host(i["url"])]
    for i in items:
        if i["audio_url"] and not allowed_host(i["audio_url"]):
            i["audio_url"] = None
    if not items:
        return jsonify(error="لم يتم العثور على وسائط قابلة للتحميل."), 404
    return jsonify(title=data.get("title") or "instagram", items=items)


@app.get("/api/debug")
def debug():
    """تشخيص: يعرض سبب الفشل وقائمة الصيغ المتاحة. افتحه في المتصفح وأرسل لي النتيجة."""
    link = request.args.get("url", "").strip()
    if not IG_URL.match(link):
        return jsonify(error="invalid url"), 400
    out = {"yt_dlp_version": yt_dlp.version.__version__,
           "cookies_file": bool(COOKIES and os.path.exists(COOKIES))}
    try:
        import imageio_ffmpeg
        out["ffmpeg"] = os.path.exists(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as e:
        out["ffmpeg"] = f"missing: {e}"
    opts = {"quiet": True, "skip_download": True, "ignore_no_formats_error": True}
    if COOKIES and os.path.exists(COOKIES):
        opts["cookiefile"] = COOKIES
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(link, download=False)
        keys = ("format_id", "ext", "vcodec", "acodec", "height", "protocol")
        out["entries"] = [
            {"id": e.get("id"), "ext": e.get("ext"),
             "formats": [{k: f.get(k) for k in keys} for f in (e.get("formats") or [])]}
            for e in (data.get("entries") or [data]) if e]
    except Exception as e:
        out["yt_dlp_error"] = ANSI.sub("", str(e))[:500]
    m = re.search(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([\w-]+)", link)
    if m:
        try:
            r = requests.get(f"https://www.instagram.com/p/{m.group(1)}/embed/captioned/",
                             headers={"User-Agent": UA}, timeout=15)
            out["embed_status"] = r.status_code
            out["embed_images_found"] = len(parse_embed(r.text)[1]) if r.ok else 0
        except Exception as e:
            out["embed_error"] = str(e)[:200]
    return jsonify(out)


def fetch_to(path, url):
    with requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                f.write(chunk)


def merged_response(video, audio, name):
    """ينزّل الصورة والصوت مؤقتاً، يدمجهما في ملف mp4 عادي، ثم يرسله."""
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "out.mp4")
    try:
        import imageio_ffmpeg  # يُستورد هنا حتى لا يتعطل الموقع إن لم تُثبَّت المكتبة
        vpath, apath = os.path.join(tmp, "v.mp4"), os.path.join(tmp, "a.m4a")
        fetch_to(vpath, video)
        fetch_to(apath, audio)
        res = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-i", vpath, "-i", apath,
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", out],
            capture_output=True, timeout=90)
        if res.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError("ffmpeg: " + res.stderr.decode(errors="ignore")[-300:])
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        app.logger.error("merge failed: %s", e)
        return jsonify(error="فشل دمج الصوت مع الفيديو.", detail=str(e)[:300]), 502

    def stream():
        try:
            with open(out, "rb") as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return Response(stream(), headers={
        "Content-Type": "video/mp4",
        "Content-Length": str(os.path.getsize(out)),
        "Content-Disposition": f'attachment; filename="{name}"',
    })


@app.get("/api/dl")
def download():
    """يمرّر الملف عبر الخادم لتجنب حظر المتصفح، ويقبل روابط CDN الخاصة بانستغرام فقط."""
    url = request.args.get("u", "")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", request.args.get("n", "instagram"))[:80]
    audio = request.args.get("a", "")
    if not allowed_host(url) or (audio and not allowed_host(audio)):
        abort(400)

    if audio:
        return merged_response(url, audio, name)

    upstream = requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=20)
    if upstream.status_code >= 400:
        return jsonify(error="رفض إنستغرام تحميل الملف.",
                       detail=f"HTTP {upstream.status_code}"), 502
    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
        "Content-Disposition": f'attachment; filename="{name}"',
    }
    if "Content-Length" in upstream.headers:
        headers["Content-Length"] = upstream.headers["Content-Length"]
    return Response(upstream.iter_content(64 * 1024), headers=headers)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
