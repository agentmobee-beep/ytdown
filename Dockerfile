# ---------------------------------------------------------------------------
# Stage 1: build the bgutil PO-token server.
# The version MUST match the bgutil-ytdlp-pot-provider plugin pinned in
# requirements.txt - the plugin refuses to work with a server whose major
# version differs from its own.
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS bgutil
ARG BGUTIL_VERSION=2.0.2
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --single-branch --branch ${BGUTIL_VERSION} \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /src
WORKDIR /src/server
RUN npm ci && npx tsc

# ---------------------------------------------------------------------------
# Stage 2: the YTDown app.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# ffmpeg is needed to join separate video+sound tracks (1080p and above)
# and to convert audio to MP3.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Node.js runtime + the prebuilt bgutil PO-token server from stage 1.
# start.sh launches the server in the background before gunicorn starts;
# the yt-dlp plugin then fetches proof-of-origin tokens from it on
# http://127.0.0.1:4416 (its default address, internal to this container).
COPY --from=bgutil /usr/local/bin/node /usr/local/bin/node
COPY --from=bgutil /src/server /opt/bgutil/server
# Deno is yt-dlp's default-enabled JavaScript runtime (>=2.3.0 needed for
# the EJS challenge solver). Node alone must be opted in; Deno needs no
# configuration, so shipping it guarantees a working runtime.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
ENV YTDOWN_POT_SERVER_HOME=/opt/bgutil/server

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "start.sh"]
