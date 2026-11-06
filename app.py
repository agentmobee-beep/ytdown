"""MegaSave - a simple YouTube video downloader web app.

Paste a public YouTube link, click Download once, and the server prepares the
best-quality MP4 (video + sound merged with ffmpeg) and sends it back as a
file download. Advanced format choices (specific qualities, MP3) are also
available.

Run locally:   ./venv/bin/python app.py
On Render:     gunicorn app:app   (see README-DEPLOY.md)

Optional environment variables:
  YTDOWN_CLIENTS        Comma-separated yt-dlp YouTube player clients to try,
                        in order (default: web,mweb,ios,android). yt-dlp
                        merges the format lists of all of them. Set it to an
                        empty value to let yt-dlp use its own built-in
                        default client selection instead.
  YTDOWN_COOKIES        Path to a cookies.txt file. Only needed if YouTube
                        blocks your server's IP ("Sign in to confirm you're
                        not a bot"). Never commit this file anywhere public.
                        The file may live on a read-only mount (e.g. a Render
                        secret file): the app copies it to a writable temp
                        location at startup, because yt-dlp writes refreshed
                        cookies back to the file when it closes.
  YTDOWN_COOKIES_B64    The cookies.txt contents as base64 (single line).
                        Takes precedence over YTDOWN_COOKIES. Use this when
                        the host cannot be trusted to mount a secret file
                        where the app can see it (on Render the secret file
                        at /etc/secrets/cookies.txt simply did not exist
                        inside this app's container - logs, 2026-11-03 -
                        while an environment variable always reaches the
                        process). The app decodes it to a writable temp
                        file at startup and logs how many cookies loaded.
  YTDOWN_POT_SERVER_HOME  Path to a bgutil PO-token server build (its
                        `server` folder). The Docker image bakes one in at
                        /opt/bgutil/server and start.sh runs it automatically
                        on http://127.0.0.1:4416, which is the address the
                        yt-dlp plugin's HTTP provider contacts by default -
                        so in Docker no setup is needed. The value is NOT
                        passed to the plugin's script provider on purpose:
                        see base_opts() for why.
  YTDOWN_POT_BASE_URL   Base URL of a bgutil PO-token HTTP server, if it is
                        NOT on the default http://127.0.0.1:4416.
  PORT                  Port to listen on (default 8000).
"""

import base64
import copy
import hashlib
import html as html_lib
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid

from flask import Flask, Response, jsonify, render_template, request, send_file, send_from_directory

import yt_dlp
from yt_dlp.utils import DownloadError

app = Flask(__name__)

FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
CLIENTS = [c.strip() for c in os.environ.get(
    "YTDOWN_CLIENTS", "web,mweb,ios,android").split(",") if c.strip()]
COOKIES = os.environ.get("YTDOWN_COOKIES") or None


def _writable_cookiefile(path):
    """Return a writable path for the cookies file.

    yt-dlp saves refreshed cookies back to the cookie file when a
    YoutubeDL context closes. Hosts often mount the configured file
    read-only (Render secret files live under /etc/secrets), and that
    write-back crashed every request with "OSError: [Errno 30]
    Read-only file system" (Render logs, 2026-11-03). Copy the file to
    a writable temp location at startup and use the copy instead.
    """
    if not path:
        return None
    try:
        dest = os.path.join(tempfile.gettempdir(), "ytdown-cookies.txt")
        if os.path.abspath(dest) != os.path.abspath(path):
            shutil.copyfile(path, dest)
        print(f"MegaSave: using cookies file {dest}", flush=True)
        return dest
    except Exception as exc:
        # Never let the copy step break startup; the original file is
        # still readable, and _close_quietly() below tolerates the
        # read-only save-back failure if the copy did not happen.
        print(f"MegaSave: cookies copy failed ({exc!r}); using {path} in place",
              flush=True)
        return path


def _cookies_from_b64(data):
    """Decode a base64 cookies.txt (env YTDOWN_COOKIES_B64) to a temp file.

    Returns the temp file path, or None if the value is missing/invalid.
    The loaded-cookie count is logged (never the values) so the server
    logs prove the cookies really reached the app.
    """
    if not data:
        return None
    try:
        raw = base64.b64decode(data.strip(), validate=True)
        dest = os.path.join(tempfile.gettempdir(), "ytdown-cookies.txt")
        with open(dest, "wb") as fh:
            fh.write(raw)
        from yt_dlp.cookies import YoutubeDLCookieJar
        jar = YoutubeDLCookieJar(dest)
        jar.load(ignore_discard=True, ignore_expires=True)
        print(f"MegaSave: cookies ready - {len(jar)} cookies loaded from "
              f"YTDOWN_COOKIES_B64 into {dest}", flush=True)
        return dest
    except Exception as exc:
        print(f"MegaSave: YTDOWN_COOKIES_B64 unusable ({exc!r})", flush=True)
        return None


COOKIES_B64 = os.environ.get("YTDOWN_COOKIES_B64") or None
COOKIEFILE = _cookies_from_b64(COOKIES_B64) or _writable_cookiefile(COOKIES)


def _close_quietly(ydl):
    """Close a YoutubeDL instance, ignoring errors from cookie save-back.

    yt-dlp saves refreshed cookies when the instance closes; if the
    cookie file cannot be written (read-only mount), close() raises
    OSError. On Render that turned a normal YouTube refusal into an
    HTTP 500 (logs, 2026-11-03), so cleanup errors are swallowed here.
    """
    try:
        ydl.close()
    except Exception:
        pass
POT_HOME = os.environ.get("YTDOWN_POT_SERVER_HOME") or None
POT_BASE_URL = os.environ.get("YTDOWN_POT_BASE_URL") or None

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


def parse_target(raw_url):
    """Resolve a pasted link to (cache_key, target_url).

    YouTube links keep the long-standing video-ID validation (playlists
    and channel pages rejected) and are normalised to the canonical
    watch URL. Any other public http(s) link is passed to yt-dlp as-is:
    the engine supports 1,000+ sites (Facebook, Instagram, X, TikTok,
    Reddit, Vimeo, ...), and an unsupported link simply gets a friendly
    error after the lookup. Private/loopback/IP-literal hosts are
    refused up front so the endpoint cannot be used to probe internal
    addresses.
    """
    url = (raw_url or "").strip()
    if not url:
        raise UserError("Please paste a video link first.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    m = re.match(r"^https?://([^/?#]+)", url, re.I)
    if not m:
        raise UserError("That does not look like a valid link.")
    host = m.group(1).lower().split(":")[0]
    if ("." not in host or host == "localhost"
            or host.endswith((".local", ".internal", ".lan"))
            or re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host)
            or ":" in host):
        raise UserError("That link is not supported. Please paste a link "
                        "to a public video page.")
    if "youtube.com" in host or "youtu.be" in host:
        video_id = parse_video_id(url)
        return "yt:" + video_id, canonical_url(video_id)
    key = "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return key, url


def base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": FFMPEG,
        "socket_timeout": 30,
        "retries": 3,
        # Where a format arrives as fragments (e.g. HLS from some player
        # clients), fetch several at once instead of one by one.
        "concurrent_fragment_downloads": 4,
        # YouTube frequently flags the IPv6 ranges of hosting providers,
        # which can make every player client return no player response at
        # all (the failure seen in the Render logs on 2026-11-02). Force
        # IPv4 so requests leave over the host's regular IPv4 address.
        "force_ipv4": True,
        # Current yt-dlp enables only Deno as a JavaScript runtime by
        # default; without any runtime it warns that YouTube extraction
        # is degraded and some formats may be missing. The Docker image
        # ships Node (for the PO-token server), so enable it explicitly.
        "js_runtimes": {"node": {}, "deno": {}},
        "extractor_args": {"youtube": {}},
    }
    if CLIENTS:
        # Only pin the player-client list when one is configured; an empty
        # YTDOWN_CLIENTS lets yt-dlp fall back to its own default clients.
        opts["extractor_args"]["youtube"]["player_client"] = CLIENTS
    # fetch_pot=always forces a PO token to be minted for every player
    # request; under the default "auto" the web_safari/mweb/tv clients
    # never request one and YouTube bot-checks them from datacenter IPs.
    if os.environ.get("YTDOWN_FETCH_POT"):
        opts["extractor_args"]["youtube"]["fetch_pot"] = [
            os.environ["YTDOWN_FETCH_POT"]]
    # The EJS challenge-solver script: the yt-dlp-ejs pip package provides
    # it locally, but the remote-components flag makes yt-dlp fetch it
    # when the packaged copy does not match the running build.
    if os.environ.get("YTDOWN_REMOTE_COMPONENTS"):
        opts["remote_components"] = [
            c.strip() for c in os.environ["YTDOWN_REMOTE_COMPONENTS"].split(",")
            if c.strip()]
    if COOKIEFILE and os.environ.get("YTDOWN_DISABLE_COOKIES") != "1":
        opts["cookiefile"] = COOKIEFILE
    # Chrome TLS impersonation (needs curl-cffi) can matter when YouTube
    # tarpits non-browser TLS fingerprints from datacenter IPs.
    if os.environ.get("YTDOWN_IMPERSONATE"):
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            opts["impersonate"] = ImpersonateTarget.from_str(
                os.environ["YTDOWN_IMPERSONATE"])
        except Exception as exc:
            print(f"MegaSave: impersonate unavailable ({exc!r})", flush=True)
    # PO-token providers (bgutil-ytdlp-pot-provider 2.0.2): deliberately do
    # NOT configure the script provider. When both providers are available
    # the plugin prioritises the script one, and on Render's free tier its
    # availability check (`node <server_home>/build/generate_once.js
    # --version`) timed out after 15s and broke extraction (Render logs,
    # 2026-11-02: subprocess.TimeoutExpired in getpot_bgutil_script.py).
    # The HTTP provider needs no configuration at all when the server
    # listens on its default http://127.0.0.1:4416 - which is how start.sh
    # runs the copy baked into the Docker image - so leaving the script
    # provider unconfigured makes the plugin use the fast HTTP one only.
    # (POT_HOME is still used by start.sh to launch that HTTP server.)
    if POT_BASE_URL:
        # Only needed when the PO-token HTTP server is somewhere other than
        # the default address (namespace/key per plugin 2.0.2 source).
        opts["extractor_args"]["youtubepot-bgutilhttp"] = {"base_url": [POT_BASE_URL]}
    return opts


def friendly_error(exc):
    msg = str(exc)
    if "Sign in to confirm" in msg or "not a bot" in msg:
        return ("That site is currently refusing requests from this server's address "
                "(its anti-bot check). Please try again later.")
    if "Unsupported URL" in msg:
        return ("That link is not supported. Please paste a link to a single "
                "public video (YouTube, Facebook, Instagram, X, TikTok, "
                "Reddit, Vimeo and many other sites work).")
    low = msg.lower()
    if "login" in low or "log in" in low or "sign in" in low or "cookies" in low:
        return ("That video needs a login to view, so our server cannot see "
                "it. Videos that are public without a login work best here.")
    if "Private video" in msg or "private" in msg.lower() and "video" in msg.lower():
        return "That video is private, so it cannot be downloaded."
    if "Video unavailable" in msg or "unavailable" in msg.lower():
        return "That video is unavailable (it may have been deleted or restricted)."
    if "live" in msg.lower() and ("event" in msg.lower() or "stream" in msg.lower()):
        return "Live streams cannot be downloaded. Please try again after the stream has ended."
    return "Something went wrong while reading that video. Please check the link and try again."


def _snapchat_spotlight_fallback(target_url):
    """Build a one-format info dict for a Snapchat Spotlight link.

    yt-dlp's SnapchatSpotlight extractor currently fails on Snapchat's
    page data ("Unable to extract next.js data"), but the public
    Spotlight page itself carries the snap's video in its og:video meta
    tag. Read that page and hand the rest of the pipeline a normal
    single-file format. Returns None when the link is not a Spotlight
    link or the page offers no video.
    """
    if "snapchat.com" not in target_url or "/spotlight/" not in target_url:
        return None
    req = urllib.request.Request(target_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/130.0 Safari/537.36"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = resp.read().decode("utf-8", "replace")
    except Exception:
        return None

    def og(prop):
        m = re.search(r'<meta property="og:%s" content="([^"]+)"'
                      % re.escape(prop), page)
        return html_lib.unescape(m.group(1)) if m else None

    video = og("video") or og("video:secure_url")
    if not video:
        return None
    try:
        width = int(og("video:width") or 0) or None
    except ValueError:
        width = None
    try:
        height = int(og("video:height") or 0) or None
    except ValueError:
        height = None
    # build_options needs a height for the row label; the label uses the
    # smaller dimension, so the width alone still labels it honestly.
    width = width or 540
    height = height or width
    snap_id = target_url.rstrip("/").rsplit("/", 1)[-1]
    return {
        "_type": "video",
        "id": snap_id,
        "title": (og("title") or "Snapchat Spotlight video")[:120],
        "extractor": "SnapchatSpotlight",
        "extractor_key": "SnapchatSpotlight",
        "webpage_url": target_url,
        "thumbnail": og("image"),
        "duration": None,
        "formats": [{
            "format_id": "spotlight-0",
            "url": video,
            "ext": "mp4",
            "vcodec": "avc1",
            "acodec": "mp4a",
            "width": width,
            "height": height,
            "protocol": "https",
        }],
    }


# ---------------------------------------------------------------------------
# YouTube egress: this host's own IP is hard-blocked by YouTube (every
# player API request answered 403, the webpage 429 - proven by verbose
# diagnostic logs on 2026-11-06 across all clients, with/without cookies,
# with PO tokens generated, and with Chrome TLS impersonation). The fix
# is a different egress for YouTube traffic only: a Cloudflare WARP
# tunnel run inside the container (wireproxy SOCKS5 on 127.0.0.1:25344,
# started by start.sh). Other platforms keep the direct connection -
# they work and must not regress. YTDOWN_PROXY overrides the proxy URL;
# YTDOWN_PROXY=off disables it. Extraction and download must use the
# SAME egress because YouTube's media URLs are bound to the IP that
# extracted them, so both paths share this decision (and the recorded
# outcome of the last extraction attempt).
# ---------------------------------------------------------------------------

_YT_PROXY_OK = {"v": None}


def _proxy_url():
    val = os.environ.get("YTDOWN_PROXY", "socks5://127.0.0.1:25344")
    return None if val.strip().lower() in ("", "off", "none") else val


def _is_youtube_url(target_url):
    return "youtube.com" in target_url or "youtu.be" in target_url


def extract_info(target_url):
    """Fetch metadata + formats with yt-dlp (no download)."""
    if _is_youtube_url(target_url) and _proxy_url() \
            and _YT_PROXY_OK["v"] is not False:
        try:
            result = _extract_info_once(target_url, use_proxy=True)
            _YT_PROXY_OK["v"] = True
            return result
        except UserError:
            # WARP path failed: fall back to the direct connection once,
            # and remember that the proxy is not the working path.
            result = _extract_info_once(target_url, use_proxy=False)
            _YT_PROXY_OK["v"] = False
            return result
    return _extract_info_once(target_url, use_proxy=False)


def _extract_info_once(target_url, use_proxy=False):
    opts = base_opts()
    if use_proxy:
        opts["proxy"] = _proxy_url()
    ydl = yt_dlp.YoutubeDL(opts)
    try:
        info = ydl.extract_info(target_url, download=False)
    except DownloadError as exc:
        info = _snapchat_spotlight_fallback(target_url)
        if info is None:
            raise UserError(friendly_error(exc))
    finally:
        _close_quietly(ydl)
    if not info:
        raise UserError("We could not read that video. Please check the link and try again.")
    if info.get("_type") == "playlist" or "entries" in info:
        raise UserError("That link opened a list of videos. Please open one "
                        "single video and paste that video's link instead.")
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
        fallback = _snapchat_spotlight_fallback(target_url)
        if fallback is not None:
            return fallback, fallback["formats"]
        raise UserError("That site did not offer any downloadable version of that video to our server. "
                        "This can happen when a site restricts a server address - please try again later.")
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


def fmt_mb(num):
    """Compact size like '33.43MB' for the option rows. None when unknown."""
    if not num:
        return None
    return f"{float(num) / 1048576:.2f}MB"


def _codec_badge(vcodec):
    v = (vcodec or "").lower()
    if v.startswith("avc1"):
        return "avc1"
    if v.startswith("vp09") or v.startswith("vp9"):
        return "vp9"
    if v.startswith("av01"):
        return "av1"
    return v.split(".")[0] or None


def build_options(formats, duration=None):
    """Turn the raw yt-dlp format list into option rows for the page.

    Returns {"audio": [...], "video": [...]}. Video rows are one per
    (resolution, frame-rate) pair, where the resolution number follows
    the smaller frame dimension - the way downloader sites label
    portrait Shorts (a 1080x1920 Short is "1080p"). Each row names the
    exact stream the server will fetch: a ready-made file when YouTube
    offers video+sound in one stream ("file"), otherwise that video
    stream merged with the best AAC audio ("merge"), mirroring the
    codec preference of _best_format_string (H.264 first). Sizes are
    estimates from YouTube's own numbers, audio included for merges.
    """
    def fsize(f):
        return f.get("filesize") or f.get("filesize_approx")

    audios = [f for f in formats
              if (f.get("vcodec") in (None, "none"))
              and (f.get("acodec") not in (None, "none"))]
    m4as = [f for f in audios if (f.get("acodec") or "").startswith("mp4a")]
    best_m4a = max(m4as, key=lambda f: f.get("abr") or 0, default=None)
    best_audio = max(audios, key=lambda f: f.get("abr") or 0, default=None)
    merge_audio = best_m4a or best_audio
    merge_audio_size = fsize(merge_audio) if merge_audio else None

    def rank(fmt):
        # Mirror the download preference: H.264 first, then the bigger
        # (higher-bitrate) stream.
        return (1 if (fmt.get("vcodec") or "").startswith("avc1") else 0,
                fsize(fmt) or 0)

    groups = {}
    for f in formats:
        vcodec = f.get("vcodec") or "none"
        if vcodec == "none" or not f.get("height"):
            continue
        p = min(f.get("width") or f["height"], f["height"])
        fps = int(round(f.get("fps") or 0)) or None
        key = (p, fps if fps and fps > 30 else None)
        if key not in groups or rank(f) > rank(groups[key]):
            groups[key] = f

    video_options = []
    for (p, fps), f in groups.items():
        has_audio = (f.get("acodec") not in (None, "none"))
        vsize = fsize(f)
        total = vsize
        if not has_audio and vsize and merge_audio_size:
            total = vsize + merge_audio_size
        video_options.append({
            "kind": "file" if has_audio else "merge",
            "format_id": f["format_id"],
            "quality": f"{p}p{fps}" if fps else f"{p}p",
            "tag": {2160: "4K", 1440: "2K"}.get(p),
            "height": f["height"], "p": p, "fps": fps,
            "ext": "mp4", "codec": _codec_badge(f.get("vcodec")),
            "size": fmt_mb(total), "size_bytes": total,
        })
    video_options.sort(key=lambda o: (-o["p"], -(o["fps"] or 0)))
    if video_options:
        video_options[0]["best"] = True

    audio_options = []
    for br in (128, 320):
        est = int(duration * br * 1000 / 8) if duration else None
        audio_options.append({
            "kind": "mp3", "bitrate": br, "quality": f"{br}k", "ext": "mp3",
            "size": fmt_mb(est), "size_bytes": est,
        })
    if best_m4a:
        abr = int(round(best_m4a.get("abr") or 0)) or 128
        audio_options.append({
            "kind": "m4a", "quality": f"{abr}k", "ext": "m4a",
            "size": fmt_mb(fsize(best_m4a)), "size_bytes": fsize(best_m4a),
        })
    return {"audio": audio_options, "video": video_options}


# ---------------------------------------------------------------------------
# Extraction cache
#
# Reading a video's formats from YouTube is the slow step (~20+ seconds:
# several player clients, proof-of-origin tokens). The signed format
# URLs stay valid for hours, so the extraction from the "search" step
# is kept briefly and reused: repeat searches answer instantly, and -
# the big win - a preparation job downloads from this cached extraction
# instead of paying for a second full extraction.
# ---------------------------------------------------------------------------

INFO_CACHE = {}
INFO_CACHE_TTL = 600
INFO_CACHE_MAX = 8


def _cache_put(video_id, info, payload):
    INFO_CACHE[video_id] = {"ts": time.time(), "info": info, "payload": payload}
    while len(INFO_CACHE) > INFO_CACHE_MAX:
        oldest = min(INFO_CACHE, key=lambda k: INFO_CACHE[k]["ts"])
        INFO_CACHE.pop(oldest, None)


def _cache_get(video_id):
    entry = INFO_CACHE.get(video_id)
    if entry and time.time() - entry["ts"] < INFO_CACHE_TTL:
        return entry
    return None


def public_info(key, target_url):
    info, formats = extract_info(target_url)
    thumb = info.get("thumbnail")
    thumbs = info.get("thumbnails") or []
    if thumbs:
        thumb = max(thumbs, key=lambda t: (t.get("width") or 0)) .get("url", thumb)
    payload = {
        "id": key,
        "site": info.get("extractor_key") or info.get("extractor") or "",
        "title": info.get("title") or "Untitled video",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "duration_string": info.get("duration_string"),
        "thumbnail": thumb,
        "webpage_url": info.get("webpage_url") or target_url,
        "options": build_options(formats, duration=info.get("duration")),
    }
    _cache_put(key, info, payload)
    return payload


def _best_format_string(target_url, height_cap=None):
    """Build the yt-dlp format string for a merged MP4 download.

    Prefer H.264 (avc1) video + AAC audio at the best available height.
    That is the most widely compatible MP4 - every phone and player
    handles it - and it matches what established downloader sites
    serve. (Pure "bestvideo" picks AV1 where available; AV1 is a newer,
    more efficient codec, so its files are roughly half the size at the
    same resolution and frame rate, but AV1/Opus in an MP4 container
    does not play everywhere.) YouTube offers avc1 only up to
    1080p-class heights, so when the top height has no avc1 - 4K is
    AV1/VP9-only - fall back to the overall best streams there.
    """
    try:
        _, formats = extract_info(target_url)
    except UserError:
        return "bestvideo+bestaudio/best"
    return _format_string_from_formats(formats, height_cap)


def _format_string_from_formats(formats, height_cap=None):
    """The _best_format_string choice, computed from an existing format list."""
    vids = [f for f in formats
            if (f.get("vcodec") not in (None, "none")) and f.get("height")]
    if height_cap:
        vids = [f for f in vids if f["height"] <= height_cap]
    if not vids:
        return "bestvideo+bestaudio/best"
    top = max(f["height"] for f in vids)
    has_avc_top = any(
        f["height"] == top and (f.get("vcodec") or "").startswith("avc1")
        for f in vids)
    if has_avc_top:
        return (f"bestvideo[height={top}][vcodec^=avc1]"
                f"+bestaudio[acodec^=mp4a]/"
                f"bestvideo[height={top}]+bestaudio/best")
    return "bestvideo+bestaudio/best"


def _make_progress_hooks(progress_cb):
    """Translate yt-dlp progress callbacks into (percent, phase) updates.

    A merged download fetches two streams (video, then audio), each
    reporting 0-100% on its own, so the reported percent is kept
    monotonic - it never runs backwards when the second stream starts.
    """
    state = {"max": 0.0}

    def phook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0.0
            state["max"] = max(state["max"], min(pct, 99.0))
            progress_cb(state["max"], "downloading")
        elif d.get("status") == "finished":
            state["max"] = max(state["max"], 95.0)
            progress_cb(state["max"], "processing")

    def pphook(d):
        if d.get("status") == "started":
            progress_cb(max(state["max"], 97.0), "processing")
        elif d.get("status") == "finished":
            progress_cb(100.0, "processing")

    return phook, pphook


def download_to_file(target_url, kind, height=None, bitrate=None, format_id=None,
                     progress_cb=None, cached_info=None):
    """Download (and merge/convert if needed). Returns (filepath, download_name).

    When cached_info (the raw extraction captured by the search step a
    moment earlier) is supplied, the download runs from it directly and
    skips a second full extraction - the single biggest time saving in
    the whole flow. Callers retry without it if it ever fails.
    """
    tmpdir = tempfile.mkdtemp(prefix="ytdown-")
    opts = base_opts()
    # Media URLs are bound to the extracting IP, so a YouTube download
    # must leave via the same egress that extracted it (WARP, unless the
    # last extraction proved the direct path is the working one).
    if _is_youtube_url(target_url) and _proxy_url() \
            and _YT_PROXY_OK["v"] is not False:
        opts["proxy"] = _proxy_url()
    opts["outtmpl"] = os.path.join(tmpdir, "%(title).100s [%(id)s].%(ext)s")
    if progress_cb is not None:
        phook, pphook = _make_progress_hooks(progress_cb)
        opts["progress_hooks"] = [phook]
        opts["postprocessor_hooks"] = [pphook]
    title_for_name = None

    if kind == "best":
        opts["format"] = (_format_string_from_formats(
            cached_info.get("formats") or []) if cached_info is not None
            else _best_format_string(target_url))
        opts["merge_output_format"] = "mp4"
    elif kind == "mp4" and height:
        opts["format"] = (_format_string_from_formats(
            cached_info.get("formats") or [], height_cap=height)
            if cached_info is not None
            else _best_format_string(target_url, height_cap=height))
        opts["merge_output_format"] = "mp4"
    elif kind == "merge" and format_id:
        # One exact video stream (the row the visitor picked) + the best
        # AAC audio, joined into a single MP4.
        opts["format"] = (f"{format_id}+bestaudio[acodec^=mp4a]/"
                          f"{format_id}+bestaudio/best")
        opts["merge_output_format"] = "mp4"
    elif kind == "file" and format_id:
        opts["format"] = format_id
    elif kind == "m4a":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    elif kind == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(bitrate or 192),
        }]
    else:
        raise UserError("Unknown download option.")

    ydl = yt_dlp.YoutubeDL(opts)
    try:
        if cached_info is not None:
            result = ydl.process_ie_result(copy.deepcopy(cached_info),
                                            download=True)
            title_for_name = result.get("title")
            downloads = result.get("requested_downloads") or []
            filename = (downloads[0].get("filepath") if downloads else None) \
                or ydl.prepare_filename(result)
        else:
            info = ydl.extract_info(target_url, download=True)
            title_for_name = info.get("title")
            filename = ydl.prepare_filename(info)
    except DownloadError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UserError(friendly_error(exc))
    finally:
        _close_quietly(ydl)

    # Post-processing may have changed the extension (merge -> .mp4, audio -> .mp3).
    wanted_ext = {"best": ".mp4", "mp4": ".mp4", "merge": ".mp4",
                  "mp3": ".mp3", "m4a": ".m4a"}.get(kind)
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


# ---------------------------------------------------------------------------
# Background preparation jobs
#
# The page flow is: pick an option -> the server prepares the file in a
# background thread while the page polls real progress -> the visitor
# presses "Download NOW" and the prepared file is served at once.
# Jobs live in memory (the app runs as a single process) and are swept
# after 30 minutes.
# ---------------------------------------------------------------------------

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_MAX_AGE = 1800
PREPARE_KINDS = {"best", "mp4", "merge", "file", "mp3", "m4a"}


def _job_gc():
    now = time.time()
    with JOBS_LOCK:
        stale = [jid for jid, job in JOBS.items()
                 if now - job["created"] > JOB_MAX_AGE]
        for jid in stale:
            job = JOBS.pop(jid)
            if job.get("tmpdir"):
                shutil.rmtree(job["tmpdir"], ignore_errors=True)


def _run_job(job_id, key, target_url, kind, height, bitrate, format_id):
    job = JOBS[job_id]

    def cb(pct, phase):
        job["percent"] = round(pct, 1)
        job["phase"] = phase

    try:
        # Fast path: download from the search step's cached extraction.
        # If that ever fails (e.g. the URLs aged out), retry once with a
        # fresh extraction before giving up.
        entry = _cache_get(key)
        cached = entry["info"] if entry else None
        try:
            filename, name, tmpdir = download_to_file(
                target_url, kind, height=height, bitrate=bitrate,
                format_id=format_id, progress_cb=cb, cached_info=cached)
        except Exception:
            # The cached-extraction fast path is only an optimisation.
            # Some extractors (e.g. TikTok's paired video/audio format
            # ids) produce cached results the downloader cannot replay
            # cleanly, so ANY failure here earns one fresh-extraction
            # attempt before the job gives up.
            if cached is None:
                raise
            traceback.print_exc()
            filename, name, tmpdir = download_to_file(
                target_url, kind, height=height, bitrate=bitrate,
                format_id=format_id, progress_cb=cb, cached_info=None)
        job.update(status="ready", percent=100, filename=filename,
                   name=name, tmpdir=tmpdir)
    except UserError as exc:
        job.update(status="error", error=str(exc))
    except Exception:
        traceback.print_exc()
        job.update(status="error",
                   error="Something went wrong while preparing that file. "
                         "Please try again.")


def _start_job(key, target_url, kind, height, bitrate, format_id):
    _job_gc()
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "working", "percent": 0,
                        "phase": "starting", "created": time.time(),
                        "error": None, "filename": None, "name": None,
                        "tmpdir": None}
    threading.Thread(target=_run_job,
                     args=(job_id, key, target_url, kind, height, bitrate,
                           format_id),
                     daemon=True).start()
    return job_id


# Pre-warming: while the visitor reads the option list, the top video
# row (the choice most people want) is already being prepared in the
# background, so pressing its Download button usually finds it ready
# or nearly ready. Capped by estimated size so a huge 4K file is not
# fetched speculatively for every search.
PREWARM = {}
PREWARM_MAX_BYTES = 120 * 1024 * 1024


def _ensure_prewarm(key, target_url, payload):
    rows = (payload.get("options") or {}).get("video") or []
    if not rows:
        return None
    top = rows[0]
    size = top.get("size_bytes")
    if size and size > PREWARM_MAX_BYTES:
        return None
    sig = (top.get("kind"), top.get("format_id"), top.get("height"))
    rec = PREWARM.get(key)
    if rec and rec["sig"] == sig:
        job = JOBS.get(rec["job_id"])
        if job and job["status"] in ("working", "ready"):
            return rec["job_id"]
    job_id = _start_job(key, target_url, top.get("kind"),
                        top.get("height"), None, top.get("format_id"))
    PREWARM[key] = {"sig": sig, "job_id": job_id}
    return job_id


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


@app.route("/creators")
def creators():
    return render_template("creators.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "MegaSave"})


SITE_ROOT = "https://ytdown-vpe5.onrender.com"


@app.route("/robots.txt")
def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n\n"
        "Sitemap: " + SITE_ROOT + "/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    pages = ["/", "/creators", "/privacy", "/terms", "/dmca"]
    urls = "".join(
        "  <url><loc>" + SITE_ROOT + p + "</loc></url>\n" for p in pages
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + urls
        + "</urlset>\n"
    )
    return Response(body, mimetype="application/xml")


@app.route("/manifest.webmanifest")
def pwa_manifest():
    return send_from_directory(
        app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json"
    )


@app.route("/sw.js")
def pwa_service_worker():
    resp = send_from_directory(
        app.static_folder, "sw.js", mimetype="application/javascript"
    )
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.route("/api/info", methods=["POST"])
def api_info():
    data = request.get_json(silent=True) or {}
    url = data.get("url") or request.form.get("url") or ""
    try:
        key, target_url = parse_target(url)
        entry = _cache_get(key)
        payload = entry["payload"] if entry else public_info(key, target_url)
        payload = dict(payload)
        payload["prewarm_job_id"] = _ensure_prewarm(key, target_url, payload)
        return jsonify(payload)
    except UserError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/prepare", methods=["POST"])
def api_prepare():
    """Start preparing one chosen option in the background."""
    data = request.get_json(silent=True) or {}
    try:
        key, target_url = parse_target(data.get("url") or "")
        kind = data.get("kind") or ""
        if kind not in PREPARE_KINDS:
            raise UserError("Unknown download option.")
        height = data.get("height")
        bitrate = data.get("bitrate")
        format_id = data.get("format_id")
        job_id = _start_job(key, target_url, kind,
                            int(height) if height else None,
                            int(bitrate) if bitrate else None,
                            format_id)
        return jsonify({"job_id": job_id})
    except UserError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "That preparation is no longer available. "
                                 "Please start again."}), 404
    size = None
    if job["status"] == "ready" and job.get("filename"):
        try:
            size = os.path.getsize(job["filename"])
        except OSError:
            size = None
    return jsonify({"status": job["status"], "percent": job["percent"],
                    "phase": job["phase"], "error": job["error"],
                    "name": job["name"], "size_bytes": size})


@app.route("/api/fetch/<job_id>")
def api_fetch(job_id):
    """Serve a file once its preparation job has finished."""
    job = JOBS.get(job_id)
    if job is None:
        return render_template("error.html", message=(
            "That download is no longer available. Please start again.")), 404
    if job["status"] != "ready":
        return render_template("error.html", message=(
            "That file is still being prepared. Please wait for it to finish.")), 409
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return serve_prepared_file(job["filename"], job["name"], job["tmpdir"])


@app.route("/download/best")
def download_best():
    """One-click download: best available MP4 with sound, merged automatically."""
    try:
        _, target_url = parse_target(request.args.get("url", ""))
        filename, name, tmpdir = download_to_file(target_url, "best")
        return serve_prepared_file(filename, name, tmpdir)
    except UserError as exc:
        return render_template("error.html", message=str(exc)), 400


@app.route("/download")
def download_option():
    """Advanced download: a specific option chosen from /api/info's list."""
    try:
        _, target_url = parse_target(request.args.get("url", ""))
        kind = request.args.get("kind", "")
        height = request.args.get("height", type=int)
        bitrate = request.args.get("bitrate", type=int)
        format_id = request.args.get("format_id")
        filename, name, tmpdir = download_to_file(
            target_url, kind, height=height, bitrate=bitrate, format_id=format_id)
        return serve_prepared_file(filename, name, tmpdir)
    except UserError as exc:
        return render_template("error.html", message=str(exc)), 400


# --------------------------------------------------------------------------
# TEMPORARY diagnostic endpoint (YouTube outage investigation, Nov 2026).
# Lets us run one extraction on THIS server with per-request overrides
# (player clients, cookies on/off, fetch_pot, impersonation) and see the
# raw yt-dlp log, which the normal API hides. Restricted to two public
# test videos and a token path. REMOVE once the outage is fixed.
# --------------------------------------------------------------------------

DIAG_TOKEN = "d7f3a91c4b2e48f0a5c6"
DIAG_ALLOWED_IDS = {"0XVyiMzCIlo", "dQw4w9WgXcQ"}


class _DiagLogger:
    def __init__(self):
        self.lines = []

    def _add(self, level, msg):
        self.lines.append(f"{level}: {msg}")
        if len(self.lines) > 400:
            self.lines = self.lines[-400:]

    def debug(self, msg):
        self._add("debug", msg)

    def info(self, msg):
        self._add("info", msg)

    def warning(self, msg):
        self._add("warning", msg)

    def error(self, msg):
        self._add("error", msg)


@app.route("/diag/<token>/ip")
def diag_ip(token):
    """Report this server's egress IP direct vs via the WARP proxy."""
    if token != DIAG_TOKEN:
        return jsonify({"ok": False}), 404

    def trace(proxy=None):
        try:
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler(
                        {"http": proxy, "https": proxy}))
                with opener.open(
                        "https://www.cloudflare.com/cdn-cgi/trace",
                        timeout=20) as resp:
                    body = resp.read().decode("utf-8", "replace")
            else:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}))
                with opener.open(
                        "https://www.cloudflare.com/cdn-cgi/trace",
                        timeout=20) as resp:
                    body = resp.read().decode("utf-8", "replace")
            fields = dict(line.split("=", 1) for line in body.splitlines()
                          if "=" in line)
            return {"ip": fields.get("ip"), "loc": fields.get("loc"),
                    "warp": fields.get("warp")}
        except Exception as exc:
            return {"error": repr(exc)}

    return jsonify({"direct": trace(), "warp": trace(_proxy_url())})


@app.route("/diag/<token>", methods=["POST"])
def diag_extract(token):
    if token != DIAG_TOKEN:
        return jsonify({"ok": False}), 404
    data = request.get_json(silent=True) or {}
    url = data.get("url") or ""
    try:
        _, target_url = parse_target(url)
    except UserError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    vid = target_url.rsplit("v=", 1)[-1]
    if vid not in DIAG_ALLOWED_IDS:
        return jsonify({"ok": False, "error": "test videos only"}), 400

    logger = _DiagLogger()
    opts = base_opts()
    opts["verbose"] = True
    opts["logger"] = logger
    opts.pop("quiet", None)
    opts.pop("no_warnings", None)
    yt_args = opts["extractor_args"].setdefault("youtube", {})
    if "clients" in data:
        clients = data["clients"]
        if clients:
            yt_args["player_client"] = clients
        else:
            yt_args.pop("player_client", None)
    if data.get("fetch_pot"):
        yt_args["fetch_pot"] = [data["fetch_pot"]]
    if data.get("cookies") is False:
        opts.pop("cookiefile", None)
    if data.get("remote_components"):
        opts["remote_components"] = ["ejs:github"]
    if data.get("impersonate"):
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            opts["impersonate"] = ImpersonateTarget.from_str(data["impersonate"])
        except Exception as exc:
            logger.error(f"impersonate setup failed: {exc!r}")
    if data.get("proxy") == "warp":
        opts["proxy"] = _proxy_url()
    ydl = yt_dlp.YoutubeDL(opts)
    try:
        info = ydl.extract_info(target_url, download=False)
        return jsonify({
            "ok": True, "title": info.get("title"),
            "n_formats": len(info.get("formats") or []),
            "log": logger.lines[-160:],
        })
    except Exception as exc:
        return jsonify({
            "ok": False, "error": f"{type(exc).__name__}: {exc}",
            "log": logger.lines[-160:],
        })
    finally:
        _close_quietly(ydl)


# --------------------------------------------------------------------------
# Telegram bot (optional): enabled only when both env vars are set.
# Webhook model - Telegram POSTs updates to us, so no extra process or
# polling is needed and the bot rides on this same service.
# --------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or None
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET") or None

if TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET:
    from telegram_bot import TelegramBot
    _telegram_bot = TelegramBot(TELEGRAM_BOT_TOKEN, SITE_ROOT)

    @app.route("/telegram/webhook/<secret>", methods=["POST"])
    def telegram_webhook(secret):
        if secret != TELEGRAM_WEBHOOK_SECRET:
            return jsonify({"ok": False}), 404
        update = request.get_json(silent=True) or {}
        threading.Thread(target=_telegram_bot.handle_update,
                         args=(update,), daemon=True).start()
        return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
