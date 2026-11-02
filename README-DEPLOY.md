# YTDown - your YouTube downloader site

A small web app with one simple job:

1. A visitor pastes a YouTube link.
2. They click the one big **Download** button - once.
3. The server fetches the video, joins picture + sound into a single MP4 in the
   **best quality the original video offers** (720p, 1080p, 1440p, all the way to
   4K when they exist), and the file saves straight to the visitor's Downloads
   folder. The download response is sent as an attachment named after the video
   title, so browsers save it instead of playing it.

Specific qualities (360p, 720p, 1080p, 2K, 4K - whatever that video actually has)
and MP3 audio (320 / 128 kbps) are still available under "More options", but no
visitor has to use them.

Private videos, members-only / paid content, DRM-protected videos, playlists,
channel pages and live streams are refused with a clear message.

---

## Run it on your own computer first

```bash
cd ytdown
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py
```

Then open http://localhost:8000 . ffmpeg must be installed
(`sudo apt install ffmpeg` on Debian/Ubuntu - it is already on many systems).
On Windows/macOS the same steps work with `python` instead of `python3`; get
ffmpeg from ffmpeg.org.

Quick health check once it is running: http://localhost:8000/health should
answer `{"ok": true, "service": "YTDown"}`.

---

## Deploy for free on Render (recommended)

Render's free web service is the simplest home for this app. Use the **Docker**
option - the included `Dockerfile` installs ffmpeg for you, which the downloader
needs to join HD video and sound. (A plain Python service on Render does **not**
include ffmpeg, so HD merging would fail there.)

1. Put this project in a GitHub repository (free at github.com). Do **not**
   upload the `venv` folder or any `cookies.txt` file - they are listed in
   `.gitignore` already if you use git.
2. Go to https://render.com , sign up (free), click **New + → Web Service** and
   connect your repository.
3. Choose environment **Docker**. Render will find the `Dockerfile` itself.
   - Build command: *(none - Docker handles it)*
   - Start command: *(none - the Dockerfile starts
     `gunicorn --bind 0.0.0.0:$PORT --workers 1 --timeout 900 app:app`)*
   - Instance type: **Free**.
4. Click **Create Web Service** and wait for the first build (a few minutes).
5. Your site is now live at an address like
   `https://ytdown-xxxx.onrender.com` - that is your free domain.
6. Open it, paste a YouTube link, and test a full download before sharing it.

Notes about the free tier: the service sleeps after ~15 idle minutes, so the
first visitor after a quiet period waits ~30-60 seconds for it to wake. Large
4K downloads also take time and memory; the free tier copes with ordinary
videos but can struggle with very long 4K files.

### Alternative: Hugging Face Spaces

Create a Space at https://huggingface.co/spaces with SDK **Docker**, upload
these files, and in the `Dockerfile` change `EXPOSE 8000` to `EXPOSE 7860`
(Hugging Face serves Docker Spaces on port 7860). Your free address will be
like `https://yourname-ytdown.hf.space`.

---

## Your own domain name (later, optional)

You can start for free on the `onrender.com` address and add a real domain any
time:

1. Buy a domain from any registrar (this is the one part that usually costs a
   small yearly fee), or use a free subdomain service.
2. In Render: your service → **Settings → Custom Domains → Add Custom Domain**,
   enter the name (for example `ytdown.example.com`).
3. Render shows you a DNS record (a CNAME pointing to your `onrender.com`
   address). Add that exact record in your registrar's DNS settings.
4. Wait for DNS to update (minutes to a few hours). Render issues the HTTPS
   certificate automatically.

---

## Before you share the site or apply for ads - fill these in

The site ships with three pages linked in the footer. Open these files and
replace every placeholder such as `[your email]`, `[date]` and
`[your name or business name]` with your own details. Do not skip this -
ad networks and visitors both look for them:

- `templates/privacy.html` - Privacy Policy
- `templates/terms.html` - Terms of Use
- `templates/dmca.html` - Copyright / DMCA contact page

## Google AdSense (read this honestly first)

- In `templates/index.html`, near the top of the `<head>`, there is a clearly
  marked comment: `GOOGLE ADSENSE: ... paste the ad code Google gives you here`.
  If Google approves your site, paste the code they give you in that spot, then
  redeploy.
- No site is pre-approved: you apply at google.com/adsense with **your own**
  Google account, add your deployed site, and Google reviews it.
- **Approval is not guaranteed, and downloader sites are often refused.**
  Google's policies restrict sites that help people infringe copyright, and
  downloading from YouTube is against YouTube's own terms except for content
  you own or are allowed to save. The filled-in Privacy / Terms / DMCA pages
  above, and the on-page notice telling users to only download content they
  own or have rights to, give you the best honest chance - but plan for the
  possibility of a "no" and do not rely on AdSense as your only income idea.
- Never click your own ads and never ask clients to - that gets accounts banned.

---

## If downloads stop working, or only 360p is offered

YouTube actively restricts requests coming from data-centre (hosting company)
IP addresses. The usual symptom: the site works, but for many videos YouTube
only offers a low quality (like 360p), or answers with its anti-bot message
"Sign in to confirm you're not a bot". The app already tries several of
YouTube's player interfaces (web, mobile web, iOS, Android) and merges
everything they return - `YTDOWN_CLIENTS` in `app.py` controls the list - but
on a restricted address the higher qualities (1080p, 4K) may simply not be
offered. Fixes, in order:

1. **Update yt-dlp** - YouTube changes things often and yt-dlp follows:
   `./venv/bin/pip install -U yt-dlp` (or rebuild/redeploy, which does the same).
2. **Cookies** - export a `cookies.txt` for youtube.com from your own browser
   (there are free "Get cookies.txt" extensions), then set the environment
   variable `YTDOWN_COOKIES` to its path on the server and restart. Use a
   spare/secondary Google account for this, keep the file secret, and never
   put it in GitHub. This usually restores full quality on restricted hosts.
3. **PO-token server** - the more technical option: build the open-source
   `bgutil-ytdlp-pot-provider` server (Node.js), then set
   `YTDOWN_POT_SERVER_HOME` to its `server` folder and restart. The plugin is
   already in `requirements.txt`.

Also set expectations honestly for clients on the site: quality can never
exceed what the original video was uploaded in.

---

## Project layout

| File | What it is |
| --- | --- |
| `app.py` | The whole backend (Flask + yt-dlp + ffmpeg). |
| `templates/index.html` | The dark-themed home page with the one-click download. |
| `templates/privacy.html`, `terms.html`, `dmca.html` | Legal pages - fill in your details. |
| `requirements.txt` | Python packages needed. |
| `Dockerfile` | Used by Render / Hugging Face Docker deploys (installs ffmpeg). |
| `start.sh` | Convenience start script. |
| `venv/` | Local-only Python environment from testing; safe to delete, never deploy it. |

Useful environment variables: `PORT`, `YTDOWN_CLIENTS`, `YTDOWN_COOKIES`,
`YTDOWN_POT_SERVER_HOME` - all documented at the top of `app.py`.
