"""A minimal ID3v2.3 writer. ffmpeg's mp3 muxer refuses to attach a picture to
an output it cannot seek in (a pipe), so the tag is built here and sent ahead
of the audio ffmpeg produces with tagging turned off.
"""
from __future__ import annotations


def _synchsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _frame(fid: str, payload: bytes) -> bytes:
    return fid.encode("ascii") + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload


def _text(fid: str, value: str) -> bytes:
    # encoding 1: UTF-16 with BOM, supported by every player since 2000
    return _frame(fid, b"\x01" + value.encode("utf-16"))


def build(tags: dict, cover: bytes | None, cover_mime: str = "image/jpeg") -> bytes:
    frames = b""
    if tags.get("title"):
        frames += _text("TIT2", str(tags["title"])[:500])
    if tags.get("artist"):
        frames += _text("TPE1", str(tags["artist"])[:500])
    if tags.get("album"):
        frames += _text("TALB", str(tags["album"])[:500])
    if tags.get("date") and len(str(tags["date"])) >= 4:
        frames += _text("TYER", str(tags["date"])[:4])
    if tags.get("comment"):
        frames += _frame("COMM", b"\x01eng" + "".encode("utf-16") + b"\x00\x00" + str(tags["comment"])[:1000].encode("utf-16"))
    if cover:
        frames += _frame("APIC", b"\x01" + cover_mime.encode("ascii") + b"\x00" + b"\x03" + "".encode("utf-16") + b"\x00\x00" + cover)
    if not frames:
        return b""
    return b"ID3\x03\x00\x00" + _synchsafe(len(frames)) + frames
