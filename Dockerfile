# ===== Stage 1: descargar ffmpeg estático (amd64) =====
FROM alpine:3.22 AS ffmpeg
RUN apk add --no-cache curl xz

ARG FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"

RUN mkdir -p /opt/ffmpeg \
  && curl -L "$FFMPEG_URL" | tar -xJ --strip-components=1 -C /opt/ffmpeg \
  && chmod +x /opt/ffmpeg/ffmpeg /opt/ffmpeg/ffprobe

# ===== Stage 2: n8n (fija versión estable) =====
FROM docker.n8n.io/n8nio/n8n:2.2.4

USER root
COPY --from=ffmpeg /opt/ffmpeg/ffmpeg  /usr/local/bin/ffmpeg
COPY --from=ffmpeg /opt/ffmpeg/ffprobe /usr/local/bin/ffprobe
RUN chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe

USER node
