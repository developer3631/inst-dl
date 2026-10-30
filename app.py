import html as htmllib
import os
import re
import subprocess
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


def best_format(info, username, index):
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
    kind = "image" if ext in ("jpg", "jpeg", "png", "webp") else "video"
    code = re.sub(r"[^\w-]", "", str(info.get("id") or ""))
    filename = f"{username}_{code}_{index}.{ext}" if code else f"{username}_{index}.{ext}"
    return {"url": url, "ext": ext, "type": kind,
            "thumb": info.get("thumbnail"), "filename": filename,
            "audio_url": audio_url}


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

    opts = {"quiet": True, "skip_download": True, "noplaylist": False}
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
        return jsonify(error="تعذر جلب المحتوى. قد يكون الحساب خاصاً أو الرابط خاطئ."), 422

    entries = data.get("entries") or [data]
    username = pick_username(data)
    items = [best_format(e, username, i + 1) for i, e in enumerate(entries) if e]
    items = [i for i in items if i["url"] and allowed_host(i["url"])]
    for i in items:
        if i["audio_url"] and not allowed_host(i["audio_url"]):
            i["audio_url"] = None
    if not items:
        return jsonify(error="لم يتم العثور على وسائط قابلة للتحميل."), 404
    return jsonify(title=data.get("title") or "instagram", items=items)


def merged_response(video, audio, name):
    """يدمج الفيديو والصوت بدون إعادة ترميز ويبثه مباشرة للمستخدم."""
    import imageio_ffmpeg  # يُستورد هنا حتى لا يتعطل الموقع إن لم تُثبَّت المكتبة
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error",
           "-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0",
           "-c", "copy", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def stream():
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()
            proc.wait()

    return Response(stream(), headers={
        "Content-Type": "video/mp4",
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

    upstream = requests.get(url, stream=True, timeout=20)
    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
        "Content-Disposition": f'attachment; filename="{name}"',
    }
    if "Content-Length" in upstream.headers:
        headers["Content-Length"] = upstream.headers["Content-Length"]
    return Response(upstream.iter_content(64 * 1024), headers=headers)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
