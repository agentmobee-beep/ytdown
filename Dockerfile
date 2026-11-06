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
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# Cloudflare WARP egress for YouTube traffic: this host's own IP is
# hard-blocked by YouTube (403 on every player API request), so YouTube
# requests leave through a free WARP tunnel instead. wgcf registers a
# WARP identity and generates the WireGuard profile; wireproxy runs it
# as a userspace SOCKS5 proxy on 127.0.0.1:25344 (no root/NET_ADMIN
# needed). Registration is attempted at build time and retried by
# start.sh at boot; if it fails, the app simply runs direct-only.
ARG WGCF_VERSION=2.3.1
ARG WIREPROXY_VERSION=1.1.3
RUN mkdir -p /opt/warp && cd /opt/warp \
    && curl -fsSL -o wgcf "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_amd64" \
    && chmod +x wgcf \
    && curl -fsSL -o wireproxy.tar.gz "https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_amd64.tar.gz" \
    && tar -xzf wireproxy.tar.gz && rm wireproxy.tar.gz \
    && mv wireproxy /usr/local/bin/wireproxy \
    && (./wgcf register --accept-tos \
        && ./wgcf generate -p wgcf-profile.conf \
        && printf '\n[Socks5]\nBindAddress = 127.0.0.1:25344\n' >> wgcf-profile.conf \
        && echo "WARP: profile registered at build time") \
       || echo "WARP: build-time registration failed; start.sh will retry"

# Node.js runtime + the prebuilt bgutil PO-token server from stage 1.
# start.sh launches the server in the background before gunicorn starts;
# the yt-dlp plugin then fetches proof-of-origin tokens from it on
# http://127.0.0.1:4416 (its default address, internal to this container).
COPY --from=bgutil /usr/local/bin/node /usr/local/bin/node
COPY --from=bgutil /src/server /opt/bgutil/server
# NOTE: no Deno in this image on purpose. Deno was tried (2026-11-06)
# but on the 512 MB free tier its per-extraction process pushed the
# container over the memory limit and the host killed it roughly every
# 2 minutes during YouTube work. Node 22 (copied above for the PO-token
# server) is enabled as yt-dlp's JS runtime instead and uses far less.
ENV YTDOWN_POT_SERVER_HOME=/opt/bgutil/server

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "start.sh"]
