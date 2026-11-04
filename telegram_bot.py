"""MegaSave Telegram bot.

Flow (mirrors the website): a user pastes a video link -> the bot searches
and replies with quality buttons -> the user taps one -> the bot prepares
the file with live progress -> the file is delivered in the chat, or, when
it exceeds Telegram's 50 MB bot-upload limit, as a download-link button.

The bot reuses the web app's own JSON API on localhost (/api/info,
/api/prepare, /api/progress, /api/fetch), so extraction, caching, formats
and merging behave exactly like the site. No third-party dependencies:
Telegram calls go through urllib.
"""

import json
import os
import re
import threading
import time
import urllib.request
import urllib.error

TG_LIMIT = int(os.environ.get("TELEGRAM_FILE_LIMIT", 49_000_000))
# ^ stay safely under Telegram's 50 MB bot upload cap (env override for tests)
PENDING_TTL = 900      # a search's quality buttons stay valid for 15 min
POLL_EVERY = 4         # seconds between progress checks
JOB_TIMEOUT = 1200     # give up on a preparation after 20 min

URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _fmt_mb(num):
    if not num:
        return ""
    return f"{num / 1_000_000:.1f} MB".replace(".0 MB", " MB")


class TelegramBot:
    def __init__(self, token, public_base):
        self.token = token
        self.api_base = (os.environ.get("TELEGRAM_API_BASE")
                         or "https://api.telegram.org").rstrip("/")
        self.public_base = public_base.rstrip("/")
        port = os.environ.get("PORT", "8000")
        self.self_base = f"http://127.0.0.1:{port}"
        self.pending = {}          # pid -> {url, title, options, ts}
        self.lock = threading.Lock()
        self.seq = 0

    # ------------------------------------------------------------------
    # Telegram Bot API
    # ------------------------------------------------------------------
    def api(self, method, payload=None, timeout=60):
        req = urllib.request.Request(
            f"{self.api_base}/bot{self.token}/{method}",
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def send_message(self, chat_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        out = self.api("sendMessage", payload)
        return (out.get("result") or {}).get("message_id")

    def edit_message(self, chat_id, message_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "message_id": message_id,
                   "text": text}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            self.api("editMessageText", payload)
        except urllib.error.HTTPError:
            pass  # "message is not modified" etc. are harmless here

    def send_document(self, chat_id, filename, data, caption):
        boundary = "----megasavebotboundary"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
            f"{chat_id}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f"{caption}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="document"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        body = head + data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{self.api_base}/bot{self.token}/sendDocument",
            data=body,
            headers={"Content-Type":
                     f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode())

    # ------------------------------------------------------------------
    # The web app's own API, on localhost
    # ------------------------------------------------------------------
    def self_call(self, path, payload=None, timeout=240):
        if payload is None:
            req = urllib.request.Request(self.self_base + path)
        else:
            req = urllib.request.Request(
                self.self_base + path,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    # ------------------------------------------------------------------
    # Update handling
    # ------------------------------------------------------------------
    def handle_update(self, update):
        try:
            if "callback_query" in update:
                self._on_callback(update["callback_query"])
            elif "message" in update:
                self._on_message(update["message"])
        except Exception as exc:  # never let one update kill the thread
            print(f"[telegram_bot] update failed: {exc!r}", flush=True)

    def _on_message(self, msg):
        chat_id = (msg.get("chat") or {}).get("id")
        text = msg.get("text") or ""
        if chat_id is None:
            return
        if text.startswith("/start"):
            self.send_message(
                chat_id,
                "👋 Welcome to MegaSave!\n\n"
                "Send me a video link (YouTube, TikTok, Instagram, "
                "Facebook, X and 1,000+ more sites) and I'll fetch it "
                "for you.\n\nJust paste the link to begin 👇")
            return
        match = URL_RE.search(text)
        if not match:
            self.send_message(
                chat_id,
                "Please send me a video link — for example a YouTube, "
                "TikTok or Instagram link — and I'll get its download "
                "options.")
            return
        url = match.group(0).rstrip(").,]")
        mid = self.send_message(chat_id, "🔎 Searching…")
        try:
            info = self.self_call("/api/info", {"url": url})
        except urllib.error.HTTPError as err:
            try:
                detail = json.loads(err.read().decode()).get("error")
            except Exception:
                detail = None
            self.edit_message(chat_id, mid,
                              f"❌ {detail or 'That link did not work.'}")
            return
        except Exception:
            self.edit_message(
                chat_id, mid,
                "❌ Something went wrong while searching. Please try "
                "again in a moment.")
            return
        options = []
        groups = info.get("options") or {}
        for opt in groups.get("video") or []:
            options.append(("video", opt))
        for opt in groups.get("audio") or []:
            options.append(("audio", opt))
        if not options:
            self.edit_message(chat_id, mid,
                              "❌ No downloadable formats were found "
                              "for that link.")
            return
        with self.lock:
            self.seq += 1
            pid = str(self.seq)
            self.pending[pid] = {"url": url,
                                 "title": info.get("title") or "Video",
                                 "options": options,
                                 "ts": time.time()}
        keyboard = []
        for idx, (group, opt) in enumerate(options):
            size = _fmt_mb(opt.get("size_bytes"))
            if group == "video":
                label = f"🎬 {opt.get('quality', '')}"
                if opt.get("tag"):
                    label += f" {opt['tag']}"
            else:
                label = f"🎵 {opt.get('ext', '').upper()} {opt.get('quality', '')}"
            if size:
                label += f" · {size}"
            if (opt.get("size_bytes") or 0) > TG_LIMIT:
                label += " · link"
            keyboard.append([{"text": label,
                              "callback_data": f"d:{pid}:{idx}"}])
        title = info.get("title") or "Video"
        site = info.get("site") or ""
        self.edit_message(
            chat_id, mid,
            f"✅ {title}\n({site})\n\nChoose a quality 👇\n"
            f"(Options marked “link” are over Telegram's 50 MB bot "
            f"limit and arrive as a download button.)",
            keyboard=keyboard)

    def _on_callback(self, query):
        data = query.get("data") or ""
        msg = query.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        mid = msg.get("message_id")
        try:
            self.api("answerCallbackQuery",
                     {"callback_query_id": query.get("id")})
        except Exception:
            pass
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "d" or chat_id is None:
            return
        with self.lock:
            entry = self.pending.get(parts[1])
        if (not entry or time.time() - entry["ts"] > PENDING_TTL
                or int(parts[2]) >= len(entry["options"])):
            self.edit_message(chat_id, mid,
                              "⌛ That search has expired. Please send "
                              "the link again.")
            return
        group, opt = entry["options"][int(parts[2])]
        threading.Thread(
            target=self._prepare_and_deliver,
            args=(chat_id, mid, entry["url"], entry["title"], opt),
            daemon=True).start()

    def _prepare_and_deliver(self, chat_id, mid, url, title, opt):
        try:
            self.edit_message(chat_id, mid, "⏳ Preparing… 0%")
            job = self.self_call("/api/prepare", {
                "url": url,
                "kind": opt.get("kind"),
                "height": opt.get("height"),
                "bitrate": opt.get("bitrate"),
                "format_id": opt.get("format_id"),
            })
            job_id = job["job_id"]
            deadline = time.time() + JOB_TIMEOUT
            shown = -1
            while True:
                prog = self.self_call(f"/api/progress/{job_id}",
                                      timeout=30)
                status = prog.get("status")
                if status == "ready":
                    size = prog.get("size_bytes")
                    name = prog.get("name") or "video.mp4"
                    break
                if status == "error":
                    self.edit_message(
                        chat_id, mid,
                        f"❌ {prog.get('error') or 'Preparation failed.'}")
                    return
                pct = int(prog.get("percent") or 0)
                if pct != shown:
                    shown = pct
                    self.edit_message(chat_id, mid,
                                      f"⏳ Preparing… {pct}%")
                if time.time() > deadline:
                    self.edit_message(
                        chat_id, mid,
                        "❌ This is taking too long. Please try again, "
                        "or use the website: "
                        "https://ytdown-vpe5.onrender.com")
                    return
                time.sleep(POLL_EVERY)
            fetch_url = f"{self.public_base}/api/fetch/{job_id}"
            if size is not None and size <= TG_LIMIT:
                self.edit_message(
                    chat_id, mid,
                    f"📤 Sending your file ({_fmt_mb(size)})…")
                # Bytes always come from this same service on localhost;
                # the public URL is only for the user-facing link below.
                with urllib.request.urlopen(
                        f"{self.self_base}/api/fetch/{job_id}",
                        timeout=600) as resp:
                    data = resp.read()
                try:
                    self.api("deleteMessage",
                             {"chat_id": chat_id, "message_id": mid})
                except Exception:
                    pass
                self.send_document(chat_id, name, data,
                                   caption=title[:1000])
            else:
                kb = [[{"text": f"⬇️ Download ({_fmt_mb(size)})",
                        "url": fetch_url}]]
                self.edit_message(
                    chat_id, mid,
                    "✅ Your file is ready!\n\n"
                    "It's over Telegram's 50 MB bot limit, so grab it "
                    "with the button below — tap soon, the link "
                    "expires after a while.",
                    keyboard=kb)
        except Exception as exc:
            print(f"[telegram_bot] prepare failed: {exc!r}", flush=True)
            try:
                self.edit_message(
                    chat_id, mid,
                    "❌ Something went wrong while preparing your "
                    "file. Please try again.")
            except Exception:
                pass
