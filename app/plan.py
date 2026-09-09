"""Decide, for one media item and a set of preferences, whether to pass the
URL through unmodified or run ffmpeg, and with which arguments. The choices
here are the ones cobalt makes: copy streams, never re-encode video, mux while
downloading, transcode audio only when asked.
"""
from __future__ import annotations

import re
import secrets
import shlex
from dataclasses import dataclass, field
from typing import Any

from .resolve import Fmt, ResolveError, formats

# --------------------------------------------------------------- options ---

MODES = ("auto", "audio", "mute", "gif")
QUALITIES = ("max", "2160", "1440", "1080", "720", "480", "360", "240", "144")
VCODECS = ("h264", "vp9", "av1")
CONTAINERS = ("auto", "mp4", "webm", "mkv")
AFORMATS = ("best", "mp3", "m4a", "opus", "ogg", "wav")
ABITRATES = ("320", "256", "192", "128", "96", "64")
NAMES = ("random", "custom", "pretty", "basic", "classic", "nerdy")

DEFAULTS = {
    "mode": "auto", "quality": "1080", "vcodec": "h264", "container": "auto",
    "aformat": "best", "abitrate": "192", "name": "random", "custom_name": "", "random_name": "",
    "metadata": True, "cover": True, "direct": False,
    "start": None, "end": None, "gif_fps": 12, "gif_width": 480,
}


def normalize(raw: dict | None) -> dict:
    """Whitelist every option; anything odd becomes the default."""
    raw = raw or {}
    o = dict(DEFAULTS)

    def choose(k, allowed):
        v = str(raw.get(k, o[k]))
        o[k] = v if v in allowed else DEFAULTS[k]

    choose("mode", MODES)
    choose("quality", QUALITIES)
    choose("vcodec", VCODECS)
    choose("container", CONTAINERS)
    choose("aformat", AFORMATS)
    choose("abitrate", ABITRATES)
    choose("name", NAMES)
    o["custom_name"] = str(raw.get("custom_name") or "")[:150]
    random_name = str(raw.get("random_name") or "")
    o["random_name"] = random_name if re.fullmatch(r"[a-f0-9]{24}", random_name) else secrets.token_hex(12)
    for k in ("metadata", "cover", "direct"):
        o[k] = bool(raw.get(k, o[k]))
    for k in ("start", "end"):
        v = raw.get(k)
        try:
            o[k] = None if v in (None, "", False) else max(0.0, float(v))
        except (TypeError, ValueError):
            o[k] = None
    if o["start"] is not None and o["end"] is not None and o["end"] <= o["start"]:
        o["end"] = None
    try:
        o["gif_fps"] = min(30, max(5, int(raw.get("gif_fps", 12))))
        o["gif_width"] = min(960, max(160, int(raw.get("gif_width", 480))))
    except (TypeError, ValueError):
        o["gif_fps"], o["gif_width"] = 12, 480
    return o


# ------------------------------------------------------------------ plan ---

MIME = {
    "mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska", "mov": "video/quicktime",
    "m4a": "audio/mp4", "mp3": "audio/mpeg", "opus": "audio/ogg", "ogg": "audio/ogg", "wav": "audio/wav",
    "flac": "audio/flac", "gif": "image/gif", "ts": "video/mp2t", "3gp": "video/3gpp",
}
MUXER = {"mp4": "mp4", "webm": "webm", "mkv": "matroska", "m4a": "ipod", "mp3": "mp3", "opus": "opus",
         "ogg": "ogg", "wav": "wav", "gif": "gif"}
FRAG = "frag_keyframe+empty_moov+default_base_moof"  # a streamable mp4: the header goes first, no seeking back


@dataclass
class Plan:
    method: str                       # proxy | ffmpeg
    ext: str
    filename: str
    label: str                        # "1080p · h264 · merged", for the page
    size: int = 0                     # estimate, 0 when unknown
    src: Fmt | None = None            # proxy: the upstream file
    inputs: list[Fmt] = field(default_factory=list)
    args: list[str] = field(default_factory=list)   # ffmpeg: everything after the inputs
    cover: str | None = None          # a thumbnail to attach, audio only
    tags: dict = field(default_factory=dict)        # mp3: written by app.id3 ahead of the stream
    fallbacks: list = field(default_factory=list)   # plans to try if this one fails on start

    @property
    def mime(self) -> str:
        return MIME.get(self.ext, "application/octet-stream")

    def describe(self) -> dict:
        return {"method": self.method, "ext": self.ext, "filename": self.filename, "label": self.label, "size": self.size}


def _ordered_codecs(pref: str) -> list[str]:
    rest = [c for c in ("h264", "vp9", "av1", "h265", "other") if c != pref]
    return [pref] + rest


def _best_video(fs: list[Fmt], o: dict) -> Fmt | None:
    """Highest quality at or under the cap, in the preferred codec if it exists there."""
    cap = 10 ** 6 if o["quality"] == "max" else int(o["quality"])
    vids = [f for f in fs if f.kind != "audio"]
    if not vids:
        return None
    under = [f for f in vids if (f.height or 0) <= cap] or [min(vids, key=lambda f: f.height or 0)]
    top = max(f.height or 0 for f in under)
    for codec in _ordered_codecs(o["vcodec"]):
        at = [f for f in under if (f.height or 0) == top and f.vcodec == codec]
        if at:
            # sdr over hdr, a plain file over a playlist, then the highest bitrate
            return max(at, key=lambda f: (not f.hdr, f.direct, f.kind == "video", f.tbr, f.fps))
    return max(under, key=lambda f: (f.height or 0, not f.hdr, f.direct, f.tbr))


def _best_audio(fs: list[Fmt], want: str | None) -> Fmt | None:
    """Best audio-only stream; the original language is preferred over dubs, then
    the codec the container supports, then bitrate."""
    auds = [f for f in fs if f.kind == "audio"]
    if not auds:
        return None
    # a known codec first (an HLS variant that reports none is less likely to work), then
    # the original track (yt-dlp marks dubs with a lower preference), then the
    # codec the container supports, a plain file over a playlist, then bitrate
    return max(auds, key=lambda f: (f.acodec != "other", f.lang_pref > 0, f.acodec == want if want else 0, f.direct, f.abr or f.tbr))


def _container(o: dict, v: Fmt, a: Fmt | None) -> str:
    if o["container"] != "auto":
        c = o["container"]
        if c == "webm" and v.vcodec in ("h264", "h265", "other"):
            return "mp4" if v.vcodec != "other" else "mkv"
        return c
    if v.vcodec in ("vp9", "av1") and (a is None or a.acodec in ("opus", "vorbis", None)) and v.ext == "webm":
        return "webm"
    if v.vcodec in ("h264", "av1", "h265") or v.ext in ("mp4", "m4v", "mov"):
        return "mp4"
    if v.vcodec == "vp9":
        return "webm" if (a is None or a.acodec == "opus") else "mp4"
    return "mkv"


def _in_args(f: Fmt, start: float | None, url: str) -> list[str]:
    """One -i and what goes before it. `url` is what ffmpeg should open: the
    loopback tunnel for a plain file, the upstream URL itself for an HLS playlist."""
    args: list[str] = []
    if f.protocol == "m3u8":
        args += ["-protocol_whitelist", "file,http,https,tcp,tls,crypto,hls"]
    else:
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if f.headers and url == f.url:
        ua = f.headers.get("User-Agent")
        if ua:
            args += ["-user_agent", ua]
        hdr = "".join(f"{k}: {v}\r\n" for k, v in f.headers.items() if k != "User-Agent")
        if hdr:
            args += ["-headers", hdr]
    if start:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", url]
    return args


def build_inputs(plan: Plan, start: float | None, urlfor=lambda f: f.url) -> list[str]:
    args: list[str] = []
    for f in plan.inputs:
        args += _in_args(f, start, urlfor(f))
    if plan.cover and not plan.tags:  # an mp3 cover goes into the tag instead, see app.id3
        args += ["-i", plan.cover]
    return args


def make(info: dict, o: dict) -> Plan:
    if info.get("is_live"):
        raise ResolveError("live streams can't be downloaded")
    fs = formats(info)
    if not fs:
        raise ResolveError("no downloadable streams at that link")
    clip = o["start"] is not None or o["end"] is not None
    meta = _meta_args(info) if o["metadata"] else ["-map_metadata", "-1"]
    dur = ["-t", f"{o['end'] - (o['start'] or 0):.3f}"] if o["end"] is not None else []
    mode = o["mode"]
    if mode == "audio" or not any(f.kind != "audio" for f in fs):
        return _audio(info, fs, o, meta, dur, clip)
    if mode == "gif":
        return _gif(info, fs, o, dur)
    v = _best_video(fs, o)
    assert v is not None
    if mode == "mute":
        return _mute(info, fs, o, v, meta, dur, clip)
    return _video(info, fs, o, v, meta, dur, clip)


def _video(info, fs, o, v: Fmt, meta, dur, clip) -> Plan:
    a = None  # a progressive stream brings its own audio
    if v.kind == "video":
        want = "opus" if _container(o, v, None) == "webm" else "aac"
        a = _best_audio(fs, want)
        if a is None:
            # no separate audio anywhere: take it from a progressive stream
            prog = [f for f in fs if f.kind == "progressive"]
            if prog:
                a = max(prog, key=lambda f: (f.direct, f.abr or f.tbr))
    ext = _container(o, v, a)
    qual = f"{v.height}p" if v.height else "video"
    size = v.size + (a.size if a and a is not v else 0)

    # the pass-through case: one plain file that is already what was asked for
    if v.kind == "progressive" and v.direct and not clip and (o["container"] == "auto" or o["container"] == v.ext or (o["container"] == "mp4" and v.ext in ("mp4", "m4v"))):
        ext = v.ext if v.ext in MIME else "mp4"
        return Plan("proxy", ext, filename(info, o, ext, qual, v.vcodec), f"{qual} · {v.vcodec} · direct", size, src=v)

    inputs = [v] + ([a] if a is not None else [])
    maps = ["-map", "0:v:0", "-map", ("1:a:0" if a is not None else "0:a:0?")]
    acodec = ["-c:a", "copy"]
    # "other" is an HLS variant that reported no codec; copy it, and fall back to mkv if the muxer refuses
    if a is not None and ext == "mp4" and a.acodec not in ("aac", "mp3", "ac3", "eac3", "opus", "other"):
        acodec = ["-c:a", "aac", "-b:a", "192k"]
    if a is not None and ext == "webm" and a.acodec not in ("opus", "vorbis"):
        acodec = ["-c:a", "libopus", "-b:a", "160k"]
    out = _out(ext)
    args = maps + ["-c:v", "copy"] + acodec + dur + meta + out
    plan = Plan("ffmpeg", ext, filename(info, o, ext, qual, v.vcodec), f"{qual} · {v.vcodec} · {'remuxed' if len(inputs) == 1 else 'merged'}", size, inputs=inputs, args=args)
    if ext != "mkv":
        # mkv takes any codec pair; the last resort when the chosen container refuses
        alt = Plan("ffmpeg", "mkv", filename(info, o, "mkv", qual, v.vcodec), f"{qual} · {v.vcodec} · merged (mkv)", size, inputs=inputs,
                   args=maps + ["-c:v", "copy", "-c:a", "copy"] + dur + meta + _out("mkv"))
        plan.fallbacks.append(alt)
    return plan


def _mute(info, fs, o, v: Fmt, meta, dur, clip) -> Plan:
    qual = f"{v.height}p" if v.height else "video"
    ext = _container(o, v, None)
    if v.kind == "video" and v.direct and not clip and (o["container"] == "auto" or o["container"] == v.ext):
        ext = v.ext if v.ext in MIME else "mp4"
        return Plan("proxy", ext, filename(info, o, ext, qual, v.vcodec, "mute"), f"{qual} · {v.vcodec} · no audio · direct", v.size, src=v)
    args = ["-map", "0:v:0", "-an", "-c:v", "copy"] + dur + meta + _out(ext)
    plan = Plan("ffmpeg", ext, filename(info, o, ext, qual, v.vcodec, "mute"), f"{qual} · {v.vcodec} · no audio", v.size, inputs=[v], args=args)
    if ext != "mkv":
        plan.fallbacks.append(Plan("ffmpeg", "mkv", filename(info, o, "mkv", qual, v.vcodec, "mute"), f"{qual} · {v.vcodec} · no audio (mkv)", v.size, inputs=[v],
                                   args=["-map", "0:v:0", "-an", "-c:v", "copy"] + dur + meta + _out("mkv")))
    return plan


def _audio(info, fs, o, meta, dur, clip) -> Plan:
    a = _best_audio(fs, {"m4a": "aac", "opus": "opus", "mp3": "mp3", "ogg": "vorbis"}.get(o["aformat"]))
    src_is_audio = a is not None
    if a is None:
        prog = [f for f in fs if f.kind == "progressive"]
        if not prog:
            raise ResolveError("there's no audio at that link")
        a = max(prog, key=lambda f: (f.direct, f.abr or f.tbr))
    fmt = o["aformat"]
    codec = a.acodec
    if fmt == "best":
        fmt = {"aac": "m4a", "opus": "opus", "mp3": "mp3", "vorbis": "ogg", "flac": "flac"}.get(codec or "", "m4a")
    unknown = codec == "other" and fmt == "m4a"  # an HLS track that didn't say; almost always aac
    copy = unknown or (fmt == "m4a" and codec == "aac") or (fmt == "opus" and codec == "opus") or (fmt == "mp3" and codec == "mp3") or (fmt == "ogg" and codec in ("vorbis", "opus")) or (fmt == "flac" and codec == "flac")
    br = o["abitrate"] + "k"
    label = f"{fmt} · {'copied' if copy else 'converted'}"

    # the pass-through case: an audio file already in the asked-for container, when no
    # tags are wanted (tags mean ffmpeg has to rewrite it)
    if copy and src_is_audio and a.direct and not clip and not o["metadata"] and a.ext == fmt:
        return Plan("proxy", fmt, filename(info, o, fmt, None, None, "audio"), label + " · direct", a.size, src=a)

    if copy:
        codec_args = ["-c:a", "copy"]
    else:
        codec_args = {"mp3": ["-c:a", "libmp3lame", "-b:a", br], "m4a": ["-c:a", "aac", "-b:a", br], "opus": ["-c:a", "libopus", "-b:a", br],
                      "ogg": ["-c:a", "libvorbis", "-b:a", br], "wav": ["-c:a", "pcm_s16le"], "flac": ["-c:a", "flac"]}[fmt]
    ext = fmt
    args = ["-map", "0:a:0", "-vn"] + codec_args + dur + meta
    cover = None
    thumb = info.get("thumbnail")
    plan = Plan("ffmpeg", ext, filename(info, o, ext, None, None, "audio"), label, a.size if copy else 0, inputs=[a], args=args)
    if fmt == "mp3" and o["metadata"]:
        # ffmpeg can't tag an mp3 it can't seek in (a pipe), so this code builds the tag:
        # it is built in the stream layer and sent ahead of untagged audio
        plan.tags = _meta_dict(info)
        plan.cover = thumb if o["cover"] else None
        plan.args = ["-map", "0:a:0", "-vn"] + codec_args + dur + ["-map_metadata", "-1", "-id3v2_version", "0"] + _out(ext)
    elif fmt == "m4a" and o["cover"] and o["metadata"] and thumb:
        cover = thumb
        plan.cover = cover
        plan.args = ["-map", "0:a:0", "-map", "1:v:0", "-c:v", "mjpeg", "-vf", "scale='min(600,iw)':-2", "-disposition:v:0", "attached_pic"] + codec_args + dur + meta + _out(ext)
        # the thumbnail may fail to fetch or decode; the audio is then sent without it
        plan.fallbacks.append(Plan("ffmpeg", ext, plan.filename, label, plan.size, inputs=[a],
                                   args=["-map", "0:a:0", "-vn"] + codec_args + dur + meta + _out(ext)))
    else:
        plan.args = args + _out(ext)
    if unknown:
        # if it wasn't aac after all, the muxer refuses; convert instead
        plan.fallbacks.append(Plan("ffmpeg", ext, plan.filename, f"{fmt} · converted", 0, inputs=[a],
                                   args=["-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", br] + dur + meta + _out(ext)))
    return plan


def _gif(info, fs, o, dur) -> Plan:
    small = dict(o, quality="480" if o["quality"] == "max" or int(o["quality"]) > 480 else o["quality"])
    v = _best_video(fs, small)
    assert v is not None
    if not dur and (info.get("duration") or 0) > 60:
        dur = ["-t", "30"]  # a gif of a whole video is nobody's wish; take the first half minute
    w, fps = o["gif_width"], o["gif_fps"]
    vf = f"fps={fps},scale='min({w},iw)':-2:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
    args = ["-map", "0:v:0", "-an", "-vf", vf] + dur + ["-f", "gif", "pipe:1"]
    return Plan("ffmpeg", "gif", filename(info, o, "gif", f"{w}w", None, "gif"), f"gif · {w}px · {fps}fps", 0, inputs=[v], args=args)


def _out(ext: str) -> list[str]:
    args = ["-f", MUXER.get(ext, ext)]
    if ext in ("mp4", "m4a", "mov"):
        args += ["-movflags", FRAG]
    if ext == "mp4":
        args += ["-strict", "-2"]  # opus in mp4 is fine but ffmpeg wants to be asked
    return args + ["pipe:1"]


def _meta_dict(info: dict) -> dict:
    m = {"title": info.get("title"), "artist": info.get("uploader") or info.get("channel"),
         "comment": info.get("webpage_url"), "album": info.get("album") or info.get("playlist_title")}
    date = info.get("upload_date")
    if date and len(date) == 8:
        m["date"] = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    return {k: v for k, v in m.items() if v}


def _meta_args(info: dict) -> list[str]:
    args: list[str] = []
    for k, v in _meta_dict(info).items():
        if v:
            args += ["-metadata", f"{k}={str(v)[:500]}"]
    return args


# -------------------------------------------------------------- filename ---

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean(s: str, n: int = 110) -> str:
    s = _BAD.sub("", s).strip(" ._")
    s = re.sub(r"\s+", " ", s)
    return (s[:n].rstrip() or "file")


def filename(info: dict, o: dict, ext: str, qual: str | None, codec: str | None, tag: str | None = None) -> str:
    style = o["name"]
    title = clean(info.get("title") or info.get("id") or "video")
    site = clean((info.get("extractor_key") or info.get("extractor") or "site").lower(), 30)
    vid = clean(str(info.get("id") or ""), 40)
    bits = [b for b in (qual, codec, tag) if b]
    if style == "random" or (style == "custom" and not o.get("custom_name", "").strip()):
        name = o.get("random_name") or secrets.token_hex(12)
    elif style == "custom":
        name = clean(o["custom_name"], 150)
        if name.lower().endswith("." + ext.lower()):
            name = name[:-(len(ext) + 1)]
    elif style == "classic":
        name = "_".join(x for x in (site, vid or None, *bits) if x)
    elif style == "basic":
        name = title
    elif style == "nerdy":
        name = f"{title} ({', '.join([*bits, site, vid])})" if (bits or vid) else title
    else:
        name = f"{title} ({', '.join(bits)})" if bits else title
    return f"{clean(name, 150)}.{ext}"


def shell(argv: list[str]) -> str:
    """For the log: the ffmpeg command with the urls trimmed."""
    return " ".join(shlex.quote(a[:60] + "…" if a.startswith("http") else a) for a in argv)
