# One image: python + yt-dlp for reading sites, ffmpeg for muxing, deno so
# yt-dlp can run YouTube's JavaScript challenges. Runs as an unprivileged user
# on a read-only root; everything writable lives under /tmp.
FROM python:3.13-slim-trixie

ARG DENO_VERSION=v2.9.6
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    DENO_DIR=/tmp/deno \
    YTDLP_CACHE=/tmp/yt-dlp-cache

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
 && curl -fsSL -A 'OpenAI File Downloader, XaiImageApiFetch/1.0' \
      -o /tmp/deno.zip "https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
 && unzip -q /tmp/deno.zip -d /usr/local/bin && chmod 755 /usr/local/bin/deno && rm /tmp/deno.zip \
 && apt-get purge -y curl unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
 && ffmpeg -version | head -1 && deno --version | head -1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static

USER 65532:65532
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
