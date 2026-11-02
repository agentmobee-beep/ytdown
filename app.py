"""YTDown - a simple YouTube video downloader web app.

Paste a public YouTube link, click Download once, and the server prepares the
best-quality MP4 (video + sound merged with ffmpeg) and sends it back as a
file download. Advanced format choices (specific qualities, MP3) are also
available.

Run locally:   ./venv/bin/python app.py
On Render:     gunicorn app:app   (see README-DEPLOY.md)

Optional environment variables:
  YTDOWN_CLIENTS        Comma-separated yt-dlp YouTube player clients to try,
                        in order (default: web,mweb,ios,android). yt-dlp
                        merges the format lists of all of them.
  YTDOWN_COOKIES        Path to a cookies.txt file. Only needed if YouTube
                        blocks your server's IP ("Sign in to confirm you're
                        not a bot"). Never commit this file anywhere public.
  YTDOWN_POT_SERVER_HOME  Path to a bgutil PO-token server build, if you run
                        one (see README-DEPLOY.md, "If downloads stop working").
  PORT                  Port to listen on (default 8000).
"""

import os
import re
import shutil
import tempfile
import threading
import uuid

from flask import Flask, jsonify, render_template, request, send_file

import yt_dlp
from yt_dlp.utils import DownloadError

app = Flask(__name__)

FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
CLIENTS = [c.strip() for c in os.environ.get(
    "YTDOWN_CLIENTS", "web,mweb,ios,android").split(",") if c.strip()]
COOKIES = os.environ.get("YTDOWN_COOKIES") or None
POT_HOME = os.environ.get("YTDOWN_POT_SERVER_HOME") or None

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
URL_PATTERNS = [
    re.compile(r"(?:youtube\.com/watch\?[^#]*\bv=|youtu\.be/|youtube\.com/shorts/|"
               r"youtube\.com/embed/|youtube\.com/live/|music\.youtube\.com/watch\?[^#]*\bv=)"
               r"([A-Za-z0-9_-]{11})"),
]


class UserError(Exception):
    """An error whose message is safe and useful to show to the visitor."""


def parse_video_id(raw_url):
    """Return the 11-char video ID for a supported single-video URL, else raise."""
    url = (raw_url or "").strip()
    if not url:
        raise UserError("Please paste a YouTube link first.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    host = re.search(r"^https?://([^/]+)", url, re.I)
    if not host or not re.search(
            r"(youtube\.com|youtu\.be)$", host.group(1).lower().removeprefix("www.").removeprefix("m.")):
        # allow music.youtube.com / www / m subdomains via loose check below
        if not host or "youtube.com" not in host.group(1).lower() and "youtu.be" not in host.group(1).lower():
            raise UserError("That does not look like a YouTube link. Please paste a link from YouTube.")
    if "/playlist" in url or re.search(r"[?&]list=[A-Za-z0-9_-]+", url) and "v=" not in url:
        raise UserError("That is a playlist link. Please open one video from the playlist and paste that video's link instead.")
    if re.search(r"youtube\.com/(channel|user|@|c/|results|feed)", url):
        raise UserError("That is a channel or listing page. Please open a single video and paste its link instead.")
    for pattern in URL_PATTERNS:
        m = pattern.search(url)
        if m and VIDEO_ID_RE.match(m.group(1)):
            return m.group(1)
    # Last resort: a bare video ID pasted on its own.
    if VIDEO_ID_RE.match(url.rsplit("/", 1)[-1]):
        return url.rsplit("/", 1)[-1]
    raise UserError("We could not find a video in that link. Links that work look like "
                    "youtube.com/watch?v=VIDEO_ID, youtu.be/VIDEO_ID or youtube.com/shorts/VIDEO_ID.")


def canonical_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


def base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": FFMPEG,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_args": {"youtube": {"player_client": CLIENTS}},
    }
    if COOKIES:
        opts["cookiefile"] = COOKIES
    if POT_HOME:
        opts["extractor_args"]["youtubepot-bgutilscript"] = {"server_home": [POT_HOME]}
    return opts


def friendly_error(exc):
    msg = str(exc)
    if "Sign in to confirm" in msg or "not a bot" in msg:
        return ("YouTube is currently refusing requests from this server's address "
                "(its anti-bot check). Please try again later. Site owners: see the "
                "cookies / PO-token notes in README-DEPLOY.md.")
    if "Private video" in msg or "private" in msg.lower() and "video" in msg.lower():
        return "That video is private, so it cannot be downloaded."
    if "Video unavailable" in msg or "unavailable" in msg.lower():
        return "That video is unavailable (it may have been deleted or restricted)."
    if "live" in msg.lower() and ("event" in msg.lower() or "stream" in msg.lower()):
        return "Live streams cannot be downloaded. Please try again after the stream has ended."
    return "Something went wrong while reading that video. Please check the link and try again."


def extract_info(video_id):
    """Fetch metadata + formats with yt-dlp (no download)."""
    try:
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            info = ydl.extract_info(canonical_url(video_id), download=False)
    except DownloadError as exc:
        raise UserError(friendly_error(exc))
    if not info:
        raise UserError("We could not read that video. Please check the link and try again.")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise UserError("That video is a live stream or a scheduled premiere. "
                        "Live content cannot be downloaded - please try again once it has ended and is posted as a normal video.")
    availability = (info.get("availability") or "").lower()
    if availability in ("private", "premium_only", "subscriber_only", "needs_auth") or info.get("login_required"):
        raise UserError("That video needs a sign-in, membership or payment, so it cannot be downloaded here.")
    formats = [f for f in info.get("formats", []) if f.get("url")]
    if any(f.get("has_drm") for f in formats):
        raise UserError("That video is protected (DRM), so it cannot be downloaded.")
    real = [f for f in formats if (f.get("vcodec") not in (None, "none")) or (f.get("acodec") not in (None, "none"))]
    if not real:
        raise UserError("YouTube did not offer any downloadable version of that video to our server. "
                        "This can happen when YouTube restricts a server address - please try again later.")
    return info, formats


def fmt_size(num):
    if not num:
        return None
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"about {size:.0f} {unit}" if size >= 100 or unit == "B" else f"about {size:.1f} {unit}"
        size /= 1024
    return None


def build_options(formats):
    """Turn the raw yt-dlp format list into friendly download options."""
    video_heights = {}
    progressive = []
    audio_best_size = None
    for f in formats:
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        height = f.get("height")
        size = f.get("filesize") or f.get("filesize_approx")
        if vcodec != "none" and height:
            entry = video_heights.setdefault(height, {"height": height, "size": None})
            if size:
                entry["size"] = max(entry["size"] or 0, size)
            if acodec != "none" and (f.get("ext") == "mp4"):
                progressive.append({
                    "kind": "file", "format_id": f["format_id"],
                    "label": f"MP4 {height}p (ready-made, no merging)",
                    "height": height, "size": fmt_size(size),
                })
        if vcodec == "none" and acodec != "none" and size:
            audio_best_size = max(audio_best_size or 0, size)

    options = []
    for height in sorted(video_heights, reverse=True):
        tag = {2160: "4K", 1440: "2K"}.get(height, "")
        label = f"MP4 {height}p" + (f" ({tag})" if tag else "") + " - video + sound, merged"
        options.append({
            "kind": "mp4", "height": height, "label": label,
            "size": fmt_size(video_heights[height]["size"]),
        })
    options.extend(sorted(progressive, key=lambda p: -p["height"]))
    options.append({"kind": "mp3", "bitrate": 320,
                    "label": "MP3 audio - 320 kbps (best quality)",
                    "size": fmt_size(audio_best_size)})
    options.append({"kind": "mp3", "bitrate": 128,
                    "label": "MP3 audio - 128 kbps (smaller file)",
                    "size": None})
    if options and options[0]["kind"] == "mp4":
        options[0]["label"] += " - best available"
    return options


def public_info(video_id):
    info, formats = extract_info(video_id)
    thumb = info.get("thumbnail")
    thumbs = info.get("thumbnails") or []
    if thumbs:
        thumb = max(thumbs, key=lambda t: (t.get("width") or 0)) .get("url", thumb)
    return {
        "id": video_id,
        "title": info.get("title") or "Untitled video",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "duration_string": info.get("duration_string"),
        "thumbnail": thumb,
        "webpage_url": info.get("webpage_url") or canonical_url(video_id),
        "options": build_options(formats),
    }


def download_to_file(video_id, kind, height=None, bitrate=None, format_id=None):
    """Download (and merge/convert if needed). Returns (filepath, download_name)."""
    tmpdir = tempfile.mkdtemp(prefix="ytdown-")
    opts = base_opts()
    opts["outtmpl"] = os.path.join(tmpdir, "%(title).100s [%(id)s].%(ext)s")
    title_for_name = None

    if kind == "best":
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
    elif kind == "mp4" and height:
        opts["format"] = (f"bestvideo[height<={height}]+bestaudio/"
                          f"best[height<={height}]/best")
        opts["merge_output_format"] = "mp4"
    elif kind == "file" and format_id:
        opts["format"] = format_id
    elif kind == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(bitrate or 192),
        }]
    else:
        raise UserError("Unknown download option.")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(canonical_url(video_id), download=True)
            title_for_name = info.get("title")
            filename = ydl.prepare_filename(info)
    except DownloadError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UserError(friendly_error(exc))

    # Post-processing may have changed the extension (merge -> .mp4, audio -> .mp3).
    wanted_ext = {"best": ".mp4", "mp4": ".mp4", "mp3": ".mp3"}.get(kind)
    if wanted_ext and not filename.endswith(wanted_ext):
        candidate = os.path.splitext(filename)[0] + wanted_ext
        if os.path.exists(candidate):
            filename = candidate
    if not os.path.exists(filename):
        files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
        files = [f for f in files if os.path.isfile(f) and not f.endswith((".part", ".ytdl"))]
        if not files:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise UserError("The download finished but no file was produced. Please try again.")
        filename = max(files, key=os.path.getsize)

    safe_title = re.sub(r"[^\w\- ]+", "", title_for_name or "video").strip()[:80] or "video"
    download_name = safe_title + os.path.splitext(filename)[1]
    return filename, download_name, tmpdir


def serve_prepared_file(filename, download_name, tmpdir):
    response = send_file(filename, as_attachment=True, download_name=download_name)

    @response.call_on_close
    def cleanup():
        threading.Timer(5.0, shutil.rmtree, args=[tmpdir], kwargs={"ignore_errors": True}).start()

    return response


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/dmca")
def dmca():
    return render_template("dmca.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "YTDown"})


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.route("/api/info", methods=["POST"])
def api_info():
    data = request.get_json(silent=True) or {}
    url = data.get("url") or request.form.get("url") or ""
    try:
        video_id = parse_video_id(url)
        return jsonify(public_info(video_id))
    except UserError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/download/best")
def download_best():
    """One-click download: best available MP4 with sound, merged automatically."""
    try:
        video_id = parse_video_id(request.args.get("url", ""))
        filename, name, tmpdir = download_to_file(video_id, "best")
        return serve_prepared_file(filename, name, tmpdir)
    except UserError as exc:
        return render_template("error.html", message=str(exc)), 400


@app.route("/download")
def download_option():
    """Advanced download: a specific option chosen from /api/info's list."""
    try:
        video_id = parse_video_id(request.args.get("url", ""))
        kind = request.args.get("kind", "")
        height = request.args.get("height", type=int)
        bitrate = request.args.get("bitrate", type=int)
        format_id = request.args.get("format_id")
        filename, name, tmpdir = download_to_file(
            video_id, kind, height=height, bitrate=bitrate, format_id=format_id)
        return serve_prepared_file(filename, name, tmpdir)
    except UserError as exc:
        return render_template("error.html", message=str(exc)), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
