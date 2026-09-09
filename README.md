# convert.plvr.net

A self-hosted video downloader and remuxer modelled on cobalt: yt-dlp reads
the site, ffmpeg muxes the streams as they download, nothing is written to
disk and nothing is kept.

- **any major site**: whatever yt-dlp supports (YouTube included, with deno for
  the JavaScript challenges).
- **video**: pick a quality cap and a codec; separate video and audio streams are
  merged as they download, a single progressive file is passed through unmodified.
- **audio**: best as-is (m4a/opus/mp3), or converted to mp3/m4a/opus/ogg/wav
  with tags and cover art.
- **mute**, **gif**, and **clip** (start/end) modes.
- **filename styles** like cobalt's: pretty, basic, classic, nerdy.
- multi-item posts (carousels, threads) show a picker.

## Run it

```sh
pip install -r requirements.txt   # needs ffmpeg on PATH, deno for YouTube
uvicorn app.main:app --port 8080
```

Env: `TOKEN_SECRET` (download links are signed; random per process if unset),
`MAX_STREAMS` / `MAX_STREAMS_PER_IP` / `RESOLVES_PER_MIN`, `COOKIES_FILE`
(a Netscape cookie jar for sites that need a login), `YTDLP_PROXY`,
`YTDLP_REMOTE_EJS=0` to stop yt-dlp fetching its newest YouTube solver scripts.

## How it ships

Pushing to `main` builds `ghcr.io/aaarrrccc/plvr-convert:<12-char sha>`. The
cluster repo (`cluster-sec`, `k8s/convert/`) pins that tag; bumping it there
deploys the new image. yt-dlp is pinned in `requirements.txt`; when a site stops
working, bump it, push, then bump the tag.
