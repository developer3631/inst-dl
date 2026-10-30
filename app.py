import os
import re
from urllib.parse import urlparse

import requests
import yt_dlp
from flask import Flask, Response, abort, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=None)
BASE = os.path.dirname(os.path.abspath(__file__))

IG_URL = re.compile(r"^https?://(www\.)?instagram\.com/(p|reel|reels|tv)/[\w-]+", re.I)
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
    if formats:
        # استبعاد ملفات الفيديو بدون صوت وملفات الصوت فقط
        both = [f for f in formats
                if f.get("vcodec") != "none" and f.get("acodec") != "none"]
        pool = both or [f for f in formats if f.get("vcodec") != "none"] or formats
        pick = max(pool, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        url, ext = pick["url"], pick.get("ext") or ext
    kind = "image" if ext in ("jpg", "jpeg", "png", "webp") else "video"
    code = re.sub(r"[^\w-]", "", str(info.get("id") or ""))
    filename = f"{username}_{code}_{index}.{ext}" if code else f"{username}_{index}.{ext}"
    return {"url": url, "ext": ext, "type": kind,
            "thumb": info.get("thumbnail"), "filename": filename}


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
    except Exception:
        return jsonify(error="تعذر جلب المحتوى. قد يكون الحساب خاصاً أو الرابط خاطئ."), 422

    entries = data.get("entries") or [data]
    username = pick_username(data)
    items = [best_format(e, username, i + 1) for i, e in enumerate(entries) if e]
    items = [i for i in items if i["url"] and allowed_host(i["url"])]
    if not items:
        return jsonify(error="لم يتم العثور على وسائط قابلة للتحميل."), 404
    return jsonify(title=data.get("title") or "instagram", items=items)


@app.get("/api/dl")
def download():
    """يمرّر الملف عبر الخادم لتجنب حظر المتصفح، ويقبل روابط CDN الخاصة بانستغرام فقط."""
    url = request.args.get("u", "")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", request.args.get("n", "instagram"))[:80]
    if not allowed_host(url):
        abort(400)

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
