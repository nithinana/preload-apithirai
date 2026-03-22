#!/usr/bin/env python3
"""
Thirai · YouTube yt-dlp streaming server
Serves watch.html and proxies YouTube video streams.

Requirements:
    pip install flask yt-dlp

Optional (for best-quality merged streams):
    Install ffmpeg and make sure it is on your PATH.

Run:
    python server.py
Then open:  http://localhost:5000/watch.html
"""

import subprocess
import sys
import os
import re
import urllib.request
import urllib.error

# ── auto-install deps ──────────────────────────────────────────────────────────
for _pkg in ("flask", "yt_dlp"):
    try:
        __import__(_pkg)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", _pkg.replace("_", "-")])

from flask import Flask, request, Response, send_from_directory, jsonify
import yt_dlp

app = Flask(__name__, static_folder=".")


# ── helpers ────────────────────────────────────────────────────────────────────

def extract_video_id(s: str):
    for p in [r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})", r"^([A-Za-z0-9_-]{11})$"]:
        m = re.search(p, s)
        if m:
            return m.group(1)
    return None


def base_ydl_opts(**extra):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    opts.update(extra)
    return opts


# ── /api/info ──────────────────────────────────────────────────────────────────

@app.route("/api/info")
def api_info():
    vid = extract_video_id(request.args.get("url", ""))
    if not vid:
        return jsonify({"error": "Invalid YouTube URL or ID"}), 400

    try:
        with yt_dlp.YoutubeDL(base_ydl_opts()) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Best combined mp4 per height
    best_by_height = {}
    for f in info.get("formats", []):
        if (f.get("vcodec", "none") != "none"
                and f.get("acodec", "none") != "none"
                and f.get("ext") == "mp4"
                and f.get("height")):
            h = f["height"]
            if h not in best_by_height or (f.get("tbr") or 0) > (best_by_height[h].get("tbr") or 0):
                best_by_height[h] = {"format_id": f["format_id"], "height": h, "ext": "mp4", "tbr": f.get("tbr")}

    fmts = sorted(best_by_height.values(), key=lambda x: x["height"], reverse=True)
    fmts.insert(0, {
        "format_id": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "height": 9999,
        "label": "Best (auto)",
    })

    return jsonify({
        "id": vid,
        "title":       info.get("title", ""),
        "thumbnail":   info.get("thumbnail", ""),
        "duration":    info.get("duration", 0),
        "uploader":    info.get("uploader", ""),
        "view_count":  info.get("view_count", 0),
        "like_count":  info.get("like_count", 0),
        "description": (info.get("description") or "")[:600],
        "formats":     fmts,
    })


# ── /api/stream ────────────────────────────────────────────────────────────────

@app.route("/api/stream")
def api_stream():
    vid = extract_video_id(request.args.get("url", ""))
    fmt = request.args.get("fmt", "best[ext=mp4]/best")
    if not vid:
        return jsonify({"error": "Invalid URL"}), 400

    try:
        with yt_dlp.YoutubeDL(base_ydl_opts(format=fmt)) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    req_fmts = info.get("requested_formats")

    if req_fmts and len(req_fmts) >= 2:
        # Separate streams → merge via ffmpeg
        cmd = [
            "ffmpeg", "-loglevel", "quiet",
            "-i", req_fmts[0]["url"],
            "-i", req_fmts[1]["url"],
            "-c", "copy",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def gen_merged():
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    proc.kill()
                except Exception:
                    pass

        return Response(gen_merged(), mimetype="video/mp4",
                        headers={"Cache-Control": "no-cache", "Transfer-Encoding": "chunked"})

    # Single combined stream → proxy with Range support
    direct_url = info.get("url")
    if not direct_url:
        return jsonify({"error": "No stream URL"}), 500

    rng = request.headers.get("Range", "bytes=0-")
    req = urllib.request.Request(direct_url, headers={
        "User-Agent": "Mozilla/5.0 (compatible)",
        "Range":      rng,
        "Referer":    "https://www.youtube.com/",
        "Origin":     "https://www.youtube.com",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as e:
        resp = e

    status  = getattr(resp, "status", None) or getattr(resp, "code", 200)
    headers = {
        "Content-Type":  resp.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": "bytes",
    }
    for hdr in ("Content-Range", "Content-Length"):
        val = resp.headers.get(hdr)
        if val:
            headers[hdr] = val

    def gen_proxy():
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            yield chunk

    return Response(gen_proxy(), status=status, headers=headers)


# ── /api/search ────────────────────────────────────────────────────────────────

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})

    try:
        with yt_dlp.YoutubeDL(base_ydl_opts(default_search="ytsearch10", extract_flat=True)) as ydl:
            info = ydl.extract_info(f"ytsearch10:{q}", download=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    results = []
    for e in (info.get("entries") or []):
        v = e.get("id", "")
        results.append({
            "id":         v,
            "title":      e.get("title", ""),
            "thumbnail":  e.get("thumbnail") or f"https://i.ytimg.com/vi/{v}/mqdefault.jpg",
            "duration":   e.get("duration", 0),
            "uploader":   e.get("uploader", ""),
            "view_count": e.get("view_count", 0),
        })

    return jsonify({"results": results})


# ── Static ─────────────────────────────────────────────────────────────────────

@app.route("/watch.html")
def serve_watch():
    return send_from_directory(".", "watch.html")

@app.route("/")
def root():
    return send_from_directory(".", "watch.html")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🎬  Thirai · YouTube Player")
    print(f"   Server : http://localhost:{port}")
    print(f"   Open   : http://localhost:{port}/watch.html\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
