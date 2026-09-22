"""Draw a post the way x.com shows it in a timeline: a 598px column at 2x,
name and handle, the text with its links in blue, the media grid, the quoted
post as a card, and the action row with its counts.

`layout` measures everything from the post's metadata and returns the size
plus where each video goes; `render` draws it, fetching the images. A video
cell is left transparent so ffmpeg can put the moving picture underneath.
"""
from __future__ import annotations

import io
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont

from . import tweet
from .resolve import assert_public

log = logging.getLogger("convert.card")

S = 2                      # device pixels per css pixel
COL = 598                  # the timeline column
PAD = 16
AVATAR = 40
GAP = 8
THEMES = {
    "dark":  {"bg": "#000000", "text": "#e7e9ea", "gray": "#71767b", "line": "#2f3336", "blue": "#1d9bf0", "ph": "#16181c"},
    "dim":   {"bg": "#15202b", "text": "#f7f9f9", "gray": "#8b98a5", "line": "#38444d", "blue": "#1d9bf0", "ph": "#1e2732"},
    "light": {"bg": "#ffffff", "text": "#0f1419", "gray": "#536471", "line": "#cfd9de", "blue": "#1d9bf0", "ph": "#eff3f4"},
}
MAX_LINES = 30             # a long post is cut with "Show more", as x.com does
MAX_LINES_QUOTE = 10
UA = tweet.UA
TWEMOJI = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@16.0.1/assets/72x72/{code}.png"


# ------------------------------------------------------------------ fonts ---

class Fonts:
    """The text faces, found on the host: Inter (or Segoe / DejaVu) for latin,
    Noto CJK, Noto Sans for the rest, and a colour emoji face as the fallback
    when twemoji can't be fetched. Glyph coverage comes from each face's cmap."""

    ROLES = {
        "regular": ["Inter-Regular.otf", "Inter-Regular.ttf", "Inter.ttc", "segoeui.ttf", "DejaVuSans.ttf"],
        "bold": ["Inter-Bold.otf", "Inter-Bold.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"],
        "cjk": ["NotoSansCJK-Regular.ttc", "NotoSansCJKjp-Regular.otf", "NotoSansJP-Regular.otf", "YuGothR.ttc", "msgothic.ttc", "NotoSansSC-VF.ttf"],
        "cjk-bold": ["NotoSansCJK-Bold.ttc", "NotoSansCJKjp-Bold.otf", "NotoSansJP-Bold.otf", "YuGothB.ttc", "msgothic.ttc", "NotoSansSC-VF.ttf"],
        "other": ["NotoSans-Regular.ttf", "NotoSans[wdth,wght].ttf", "segoeui.ttf", "DejaVuSans.ttf"],
        "other-bold": ["NotoSans-Bold.ttf", "NotoSans[wdth,wght].ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"],
        "emoji": ["NotoColorEmoji.ttf", "seguiemj.ttf"],
    }
    DIRS = [d for d in os.environ.get("FONT_DIRS", "/usr/share/fonts:C:/Windows/Fonts").split(":") if d] if os.name != "nt" else \
           [d for d in os.environ.get("FONT_DIRS", "C:/Windows/Fonts;/usr/share/fonts").split(";") if d]

    def __init__(self):
        self.paths: dict[str, str | None] = {}
        self.cmaps: dict[str, set[int]] = {}
        self._fonts: dict[tuple, Any] = {}
        self._lock = threading.Lock()
        index: dict[str, str] = {}
        for d in self.DIRS:
            for p in glob(os.path.join(d, "**", "*"), recursive=True):
                index.setdefault(os.path.basename(p), p)
        for role, names in self.ROLES.items():
            self.paths[role] = next((index[n] for n in names if n in index), None)
        if not self.paths["regular"]:
            log.warning("no latin font found in %s; text will be drawn with Pillow's default", self.DIRS)
        log.info("fonts: %s", {k: os.path.basename(v) if v else None for k, v in self.paths.items()})

    def cmap(self, role: str) -> set[int]:
        p = self.paths.get(role)
        if not p:
            return set()
        if p not in self.cmaps:
            try:
                from fontTools.ttLib import TTFont
                f = TTFont(p, lazy=True, fontNumber=0)
                cm = f.getBestCmap() or {}
                self.cmaps[p] = set(cm.keys())
            except Exception as e:
                log.warning("cmap of %s unreadable: %s", p, e)
                self.cmaps[p] = set()
        return self.cmaps[p]

    def role_for(self, ch: str, bold: bool) -> str:
        cp = ord(ch)
        base = "bold" if bold else "regular"
        if cp < 0x2000 or cp in self.cmap(base):
            return base
        for r in (("cjk-bold", "cjk") if bold else ("cjk",)) if _is_cjk(cp) else ():
            if cp in self.cmap(r):
                return r
        for r in (("other-bold", "other") if bold else ("other",)):
            if cp in self.cmap(r):
                return r
        for r in (("cjk-bold", "cjk") if bold else ("cjk",)):
            if cp in self.cmap(r):
                return r
        return base

    def font(self, role: str, px: float):
        key = (role, round(px * 4) / 4)
        with self._lock:
            f = self._fonts.get(key)
            if f is None:
                p = self.paths.get(role) or self.paths.get("regular")
                try:
                    f = ImageFont.truetype(p, int(round(px))) if p else ImageFont.load_default(size=int(round(px)))
                    if p and "VF" in os.path.basename(p) or (p and "[" in os.path.basename(p)):
                        try:
                            f.set_variation_by_axes([700 if "bold" in role else 400])
                        except Exception:
                            pass
                except OSError:
                    f = ImageFont.load_default(size=int(round(px)))
                self._fonts[key] = f
        return f


_fonts: Fonts | None = None


def fonts() -> Fonts:
    global _fonts
    if _fonts is None:
        _fonts = Fonts()
    return _fonts


def _is_cjk(cp: int) -> bool:
    return (0x2E80 <= cp <= 0x9FFF or 0xAC00 <= cp <= 0xD7AF or 0xF900 <= cp <= 0xFAFF or 0xFE30 <= cp <= 0xFE4F
            or 0xFF00 <= cp <= 0xFFEF or 0x20000 <= cp <= 0x3FFFF or 0x3040 <= cp <= 0x30FF)


# ------------------------------------------------------------------ emoji ---

def _is_emoji_base(cp: int) -> bool:
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299)
            or 0x2190 <= cp <= 0x21FF or 0x2300 <= cp <= 0x23FF or 0x25A0 <= cp <= 0x25FF or 0x2934 <= cp <= 0x2935 or 0x1F1E6 <= cp <= 0x1F1FF)


_MOD = {0xFE0F, 0x20E3} | set(range(0x1F3FB, 0x1F400)) | set(range(0xE0020, 0xE0080))
_TEXTY = set(range(0x2190, 0x2200)) | set(range(0x2300, 0x2400)) | set(range(0x25A0, 0x2600)) | {0x00A9, 0x00AE, 0x2122, 0x2139, 0x203C, 0x2049, 0x3030, 0x303D}


def segment(s: str) -> list[tuple[str, str]]:
    """Split into ('emoji', cluster) and ('text', run) pieces."""
    out: list[tuple[str, str]] = []
    i, n, buf = 0, len(s), []
    while i < n:
        cp = ord(s[i])
        keycap = s[i] in "#*0123456789" and i + 1 < n and (ord(s[i + 1]) == 0xFE0F or ord(s[i + 1]) == 0x20E3)
        if keycap or (_is_emoji_base(cp) and not (cp in _TEXTY and not (i + 1 < n and ord(s[i + 1]) == 0xFE0F))):
            j = i + 1
            if 0x1F1E6 <= cp <= 0x1F1FF and j < n and 0x1F1E6 <= ord(s[j]) <= 0x1F1FF:
                j += 1
            while j < n:
                c = ord(s[j])
                if c in _MOD:
                    j += 1
                elif c == 0x200D and j + 1 < n:
                    j += 2
                    while j < n and ord(s[j]) in _MOD:
                        j += 1
                else:
                    break
            if buf:
                out.append(("text", "".join(buf)))
                buf = []
            out.append(("emoji", s[i:j]))
            i = j
        else:
            buf.append(s[i])
            i += 1
    if buf:
        out.append(("text", "".join(buf)))
    return out


def twemoji_code(cluster: str) -> str:
    cps = [ord(c) for c in cluster]
    if 0x200D not in cps:
        cps = [c for c in cps if c != 0xFE0F]
    return "-".join(f"{c:x}" for c in cps)


# ------------------------------------------------------------------ fetch ---

_img_cache: dict[str, tuple[float, bytes | None]] = {}
_img_lock = threading.Lock()
IMG_TTL = 15 * 60
IMG_MAX = 15 << 20


def fetch_bytes(url: str, timeout: float = 12) -> bytes | None:
    with _img_lock:
        hit = _img_cache.get(url)
        if hit and time.time() - hit[0] < IMG_TTL:
            return hit[1]
    data = None
    try:
        assert_public(url)
        with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=timeout) as c:
            r = c.get(url)
            if r.status_code < 400 and len(r.content) <= IMG_MAX:
                data = r.content
    except Exception as e:
        log.debug("fetch %s: %s", url[:80], type(e).__name__)
    with _img_lock:
        if len(_img_cache) > 300:
            for k in list(_img_cache)[:100]:
                del _img_cache[k]
        _img_cache[url] = (time.time(), data)
    return data


def fetch_image(url: str | None) -> Image.Image | None:
    if not url:
        return None
    b = fetch_bytes(url)
    if not b:
        return None
    try:
        im = Image.open(io.BytesIO(b))
        im.load()
        return im.convert("RGBA")
    except Exception:
        return None


def emoji_image(cluster: str, px: int) -> Image.Image | None:
    im = fetch_image(TWEMOJI.format(code=twemoji_code(cluster)))
    if im is None:
        p = fonts().paths.get("emoji")
        if p:
            try:
                f = ImageFont.truetype(p, 109)
                box = f.getbbox(cluster, embedded_color=True)
                tmp = Image.new("RGBA", (max(1, box[2] - box[0]), max(1, box[3] - box[1])), (0, 0, 0, 0))
                ImageDraw.Draw(tmp).text((-box[0], -box[1]), cluster, font=f, embedded_color=True)
                im = tmp
            except Exception:
                im = None
    if im is None:
        return None
    return im.resize((px, px), Image.LANCZOS)


# ---------------------------------------------------------------- numbers ---

def count(n: int | None) -> str:
    if n is None or n <= 0:
        return ""
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "K"
    if n < 1_000_000:
        return f"{n // 1000}K"
    if n < 10_000_000:
        return f"{n / 1e6:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{n // 1_000_000}M"


# ------------------------------------------------------------------ paths ---

def _arc_points(x1, y1, rx, ry, rot, large, sweep, x2, y2, n=12):
    """Endpoint-parameterised SVG arc to polyline."""
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    phi = math.radians(rot)
    cph, sph = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cph * dx + sph * dy, -sph * dx + cph * dy
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    k = math.sqrt(max(0, num / den)) if den else 0
    if large == sweep:
        k = -k
    cxp, cyp = k * rx * y1p / ry, -k * ry * x1p / rx
    cx, cy = cph * cxp - sph * cyp + (x1 + x2) / 2, sph * cxp + cph * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        d = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return d
    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    pts = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        px, py = rx * math.cos(t), ry * math.sin(t)
        pts.append((cph * px - sph * py + cx, sph * px + cph * py + cy))
    return pts


def path_polylines(d: str) -> list[list[tuple[float, float]]]:
    """A small SVG path reader: M L H V C Q A Z, absolute and relative."""
    toks = re.findall(r"[MLHVCQAZSTmlhvcqazst]|-?\d*\.?\d+(?:e-?\d+)?", d)
    out: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x = y = sx = sy = 0.0
    cx2 = cy2 = None   # the last cubic control point, for S/s
    qx = qy = None     # the last quadratic control point, for T/t
    i, cmd = 0, ""
    nums = lambda k: [float(toks[i + j]) for j in range(k)]
    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur:
                    cur.append((sx, sy))
                    out.append(cur)
                    cur = []
                x, y = sx, sy
                continue
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            nx, ny = nums(2); i += 2
            if rel: nx, ny = x + nx, y + ny
            if cur: out.append(cur)
            cur = [(nx, ny)]; x, y = sx, sy = nx, ny
            cmd = "l" if rel else "L"
        elif c == "L":
            nx, ny = nums(2); i += 2
            if rel: nx, ny = x + nx, y + ny
            cur.append((nx, ny)); x, y = nx, ny
        elif c == "H":
            nx = nums(1)[0]; i += 1
            if rel: nx += x
            cur.append((nx, y)); x = nx
        elif c == "V":
            ny = nums(1)[0]; i += 1
            if rel: ny += y
            cur.append((x, ny)); y = ny
        elif c in ("C", "S"):
            if c == "C":
                x1, y1, x2, y2, nx, ny = nums(6); i += 6
                if rel: x1, y1, x2, y2, nx, ny = x + x1, y + y1, x + x2, y + y2, x + nx, y + ny
            else:
                x2, y2, nx, ny = nums(4); i += 4
                if rel: x2, y2, nx, ny = x + x2, y + y2, x + nx, y + ny
                x1, y1 = (2 * x - cx2, 2 * y - cy2) if cx2 is not None else (x, y)
            for k in range(1, 13):
                t_ = k / 12
                bx = (1 - t_) ** 3 * x + 3 * (1 - t_) ** 2 * t_ * x1 + 3 * (1 - t_) * t_ ** 2 * x2 + t_ ** 3 * nx
                by = (1 - t_) ** 3 * y + 3 * (1 - t_) ** 2 * t_ * y1 + 3 * (1 - t_) * t_ ** 2 * y2 + t_ ** 3 * ny
                cur.append((bx, by))
            x, y = nx, ny
            cx2, cy2 = x2, y2
            continue
        elif c in ("Q", "T"):
            if c == "Q":
                x1, y1, nx, ny = nums(4); i += 4
                if rel: x1, y1, nx, ny = x + x1, y + y1, x + nx, y + ny
            else:
                nx, ny = nums(2); i += 2
                if rel: nx, ny = x + nx, y + ny
                x1, y1 = (2 * x - qx, 2 * y - qy) if qx is not None else (x, y)
            for k in range(1, 13):
                t_ = k / 12
                cur.append(((1 - t_) ** 2 * x + 2 * (1 - t_) * t_ * x1 + t_ ** 2 * nx, (1 - t_) ** 2 * y + 2 * (1 - t_) * t_ * y1 + t_ ** 2 * ny))
            x, y = nx, ny
            qx, qy = x1, y1
            continue
        elif c == "A":
            rx, ry, rot, large, sweep, nx, ny = nums(7); i += 7
            if rel: nx, ny = x + nx, y + ny
            cur += _arc_points(x, y, rx, ry, rot, bool(large), bool(sweep), nx, ny)
            x, y = nx, ny
        else:
            i += 1
        cx2 = cy2 = qx = qy = None
    if cur:
        out.append(cur)
    return out


ICONS = {
    "reply": "M1.751 10c0-4.42 3.584-8 8.005-8h4.366c4.49 0 8.129 3.64 8.129 8.13 0 2.96-1.607 5.68-4.196 7.11l-8.054 4.46v-3.69h-.067c-4.49.1-8.183-3.51-8.183-8.01zm8.005-6c-3.317 0-6.005 2.69-6.005 6 0 3.37 2.77 6.08 6.138 6.01l.351-.01h1.761v2.3l5.087-2.81c1.951-1.08 3.163-3.13 3.163-5.36 0-3.39-2.744-6.13-6.129-6.13H9.756z",
    "repost": "M4.5 3.88l4.432 4.14-1.364 1.46L5.5 7.55V16c0 1.1.896 2 2 2H13v2H7.5c-2.209 0-4-1.79-4-4V7.55L1.432 9.48.068 8.02 4.5 3.88zM16.5 6H11V4h5.5c2.209 0 4 1.79 4 4v8.45l2.068-1.93 1.364 1.46-4.432 4.14-4.432-4.14 1.364-1.46 2.068 1.93V8c0-1.1-.896-2-2-2z",
    "like": "M16.697 5.5c-1.222-.06-2.679.51-3.89 2.16l-.805 1.09-.806-1.09C9.984 6.01 8.526 5.44 7.304 5.5c-1.243.07-2.349.78-2.91 1.91-.552 1.12-.633 2.78.479 4.82 1.074 1.97 3.257 4.27 7.129 6.61 3.87-2.34 6.052-4.64 7.126-6.61 1.111-2.04 1.03-3.7.477-4.82-.561-1.13-1.666-1.84-2.908-1.91zm4.187 7.69c-1.351 2.48-4.001 5.12-8.379 7.67l-.503.3-.504-.3c-4.379-2.55-7.029-5.19-8.382-7.67-1.36-2.5-1.41-4.86-.514-6.67.887-1.79 2.647-2.91 4.601-3.01 1.651-.09 3.368.56 4.798 2.01 1.429-1.45 3.146-2.1 4.796-2.01 1.954.1 3.714 1.22 4.601 3.01.896 1.81.846 4.17-.514 6.67z",
    "views": "M8.75 21V3h2v18h-2zM18 21V8.5h2V21h-2zM4 21l.004-10h2L6 21H4zm9.248 0v-7h2v7h-2z",
    "bookmark": "M4 4.5C4 3.12 5.119 2 6.5 2h11C18.881 2 20 3.12 20 4.5v18.44l-8-5.71-8 5.71V4.5zM6.5 4c-.276 0-.5.22-.5.5v14.56l6-4.29 6 4.29V4.5c0-.28-.224-.5-.5-.5h-11z",
    "share": "M12 2.59l5.7 5.7-1.41 1.42L13 6.41V16h-2V6.41l-3.3 3.3-1.41-1.42L12 2.59zM21 15l-.02 3.51c0 1.38-1.12 2.49-2.5 2.49H5.5C4.11 21 3 19.88 3 18.5V15h2v3.5c0 .28.22.5.5.5h12.98c.28 0 .5-.22.5-.5L19 15h2z",
    "verified": "M22.25 12c0-1.43-.88-2.67-2.19-3.34.46-1.39.2-2.9-.81-3.91s-2.52-1.27-3.91-.81c-.66-1.31-1.91-2.19-3.34-2.19s-2.67.88-3.33 2.19c-1.4-.46-2.91-.2-3.92.81s-1.26 2.52-.8 3.91c-1.31.67-2.2 1.91-2.2 3.34s.89 2.67 2.2 3.34c-.46 1.39-.21 2.9.8 3.91s2.52 1.26 3.91.81c.67 1.31 1.91 2.19 3.34 2.19s2.68-.88 3.34-2.19c1.39.45 2.9.2 3.91-.81s1.27-2.52.81-3.91c1.31-.67 2.19-1.91 2.19-3.34zm-11.71 4.2L6.8 12.46l1.41-1.42 2.26 2.26 4.8-5.23 1.47 1.36-6.2 6.77z",
}


def icon(kind: str, px: int, color: str) -> Image.Image:
    """A filled 24-box glyph, drawn at 4x and shrunk for smooth edges."""
    ss = 4
    im = Image.new("RGBA", (px * ss, px * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    k = px * ss / 24
    for poly in path_polylines(ICONS[kind]):
        pts = [(x * k, y * k) for x, y in poly]
        if len(pts) >= 3:
            d.polygon(pts, fill=color)
    # even-odd fill for the hollow glyphs: subtract the inner contours
    polys = path_polylines(ICONS[kind])
    if len(polys) > 1 and kind in ("reply", "like", "bookmark", "verified"):
        mask = Image.new("L", im.size, 0)
        md = ImageDraw.Draw(mask)
        for i, poly in enumerate(polys):
            pts = [(x * k, y * k) for x, y in poly]
            if len(pts) >= 3:
                # xor: paint alternately
                tmp = Image.new("L", im.size, 0)
                ImageDraw.Draw(tmp).polygon(pts, fill=255)
                from PIL import ImageChops
                mask = ImageChops.logical_xor(mask.convert("1"), tmp.convert("1")).convert("L")
        col = Image.new("RGBA", im.size, color)
        im = Image.composite(col, Image.new("RGBA", im.size, (0, 0, 0, 0)), mask)
    return im.resize((px, px), Image.LANCZOS)


# ----------------------------------------------------------------- layout ---

@dataclass
class Cell:
    x: int
    y: int
    w: int
    h: int
    fit: str            # contain | cover
    item: dict
    radius: int
    corners: tuple = (True, True, True, True)
    master: bool = False


@dataclass
class Layout:
    width: int
    height: int
    theme: dict
    ops: list = field(default_factory=list)
    cells: list[Cell] = field(default_factory=list)


class Text:
    """Wrapped rich text: words, CJK characters and emoji placed on lines."""

    def __init__(self, spans: list[dict], size: float, lh: float, bold: bool, color: str, link: str, width: float, max_lines: int, more_color: str):
        self.size, self.lh, self.width = size, lh, width
        F = fonts()
        atoms: list[dict] = []   # {kind: text|emoji|space|nl, s, role, color, w, brk}
        for sp in spans:
            col = link if sp["link"] else color
            for kind, piece in segment(sp["text"]):
                if kind == "emoji":
                    atoms.append({"kind": "emoji", "s": piece, "w": size * 1.2 + 2, "color": col, "role": None})
                    continue
                for tok in re.findall(r"\n|[ \t]+|[^\s]+", piece):
                    if tok == "\n":
                        atoms.append({"kind": "nl", "s": "", "w": 0, "color": col, "role": None})
                    elif tok.isspace():
                        atoms.append({"kind": "space", "s": " ", "w": F.font("bold" if bold else "regular", size * S).getlength(" ") / S, "color": col, "role": "bold" if bold else "regular"})
                    else:
                        # split the word into runs by font, and let CJK break per character
                        run, role = [], None
                        for ch in tok:
                            r = F.role_for(ch, bold)
                            cj = _is_cjk(ord(ch))
                            if cj:
                                if run:
                                    atoms.append(self._text(F, "".join(run), role, col, False))
                                    run, role = [], None
                                atoms.append(self._text(F, ch, r, col, True))
                                continue
                            if role is not None and r != role:
                                atoms.append(self._text(F, "".join(run), role, col, False))
                                run = []
                            run.append(ch); role = r
                        if run:
                            atoms.append(self._text(F, "".join(run), role, col, False))
        self.lines: list[list[dict]] = []
        self.truncated = False
        line: list[dict] = []
        used = 0.0
        for a in atoms:
            if a["kind"] == "nl":
                self.lines.append(line); line, used = [], 0.0
                continue
            if a["kind"] == "space":
                if line:
                    line.append(a); used += a["w"]
                continue
            if used + a["w"] <= width or not line:
                if a["w"] > width and a["kind"] == "text":
                    # a single word wider than the column: break it by character
                    for ch in a["s"]:
                        piece = self._text(F, ch, a["role"], a["color"], True)
                        if used + piece["w"] > width and line:
                            self.lines.append(line); line, used = [], 0.0
                        line.append(piece); used += piece["w"]
                    continue
                line.append(a); used += a["w"]
            else:
                while line and line[-1]["kind"] == "space":
                    used -= line.pop()["w"]
                self.lines.append(line); line, used = [a], a["w"]
        if line:
            self.lines.append(line)
        if len(self.lines) > max_lines:
            self.lines = self.lines[:max_lines]
            self.truncated = True
            self.lines.append([self._text(F, "Show more", "regular", more_color, False)])
        self.height = len(self.lines) * lh if self.lines else 0

    def _text(self, F, s, role, color, brk):
        return {"kind": "text", "s": s, "role": role, "color": color, "w": F.font(role, self.size * S).getlength(s) / S, "brk": brk}

    @classmethod
    def make(cls, spans, size, lh, bold, color, link, width, max_lines, more_color):
        return cls(spans, size, lh, bold, color, link, width, max_lines, more_color)


def _line(s: str, bold: bool, size: float, color: str, maxw: float) -> tuple[Text, float]:
    """One line of text with per-character font fallback, cut with an
    ellipsis to fit `maxw`; returns it and its width in css px."""
    def build(t):
        return Text.make([{"text": t, "link": False}], size, 20, bold, color, color, 10 ** 6, 1, color)
    t = build(s)
    w = sum(a["w"] for a in t.lines[0]) if t.lines else 0
    while w > maxw and len(s) > 1:
        s = s[:-1].rstrip()
        t = build(s + "…")
        w = sum(a["w"] for a in t.lines[0]) if t.lines else 0
    return t, w


def media_boxes(items: list[dict], x: float, y: float, w: float, corners: tuple) -> tuple[list[tuple], float]:
    """Where each media item goes, in css px, and the block's height."""
    n = len(items)
    gap = 2
    if n == 1:
        it = items[0]
        iw, ih = it.get("w") or 16, it.get("h") or 9
        if it["kind"] == "photo":
            h = min(w * ih / iw, w * 1.4)
            return [(it, x, y, w, h, "cover")], h
        h = min(w * ih / iw, w * 1.6)
        return [(it, x, y, w, h, "contain")], h
    hw = (w - gap) / 2
    if n == 2:
        h = hw * 8 / 7
        return [(items[0], x, y, hw, h, "cover"), (items[1], x + hw + gap, y, hw, h, "cover")], h
    h = w * 9 / 16
    hh = (h - gap) / 2
    if n == 3:
        return [(items[0], x, y, hw, h, "cover"), (items[1], x + hw + gap, y, hw, hh, "cover"), (items[2], x + hw + gap, y + hh + gap, hw, hh, "cover")], h
    return [(items[0], x, y, hw, hh, "cover"), (items[1], x + hw + gap, y, hw, hh, "cover"),
            (items[2], x, y + hh + gap, hw, hh, "cover"), (items[3], x + hw + gap, y + hh + gap, hw, hh, "cover")], h


def layout(post: dict, theme: str = "dark", stats: bool = True, now: float | None = None, max_lines: int = MAX_LINES) -> Layout:
    T = THEMES.get(theme, THEMES["dark"])
    L = Layout(COL * S, 0, T)
    ops = L.ops
    master = [None]

    def px(v):
        return int(round(v * S))

    def media_block(items, x, y, w, corners, in_quote):
        boxes, h = media_boxes(items, x, y, w, corners)
        radius = 16
        for it, bx, by, bw, bh, fit in boxes:
            single = len(boxes) == 1
            if single:
                cr = corners
            else:
                # per-cell rounding: only the outer corners of the grid
                cr = (bx == x and by == y and corners[0], bx + bw >= x + w - 0.5 and by == y and corners[1],
                      bx + bw >= x + w - 0.5 and by + bh >= y + h - 0.5 and corners[2], bx == x and by + bh >= y + h - 0.5 and corners[3])
            if it["kind"] == "photo":
                ops.append(("image", it["url"], px(bx), px(by), px(bw), px(bh), px(radius), cr, "cover"))
            else:
                cell = Cell(px(bx), px(by), px(bw), px(bh), fit, it, px(radius), cr)
                if master[0] is None and it["kind"] == "video":
                    cell.master = True; master[0] = cell
                L.cells.append(cell)
                ops.append(("poster", it.get("poster"), px(bx), px(by), px(bw), px(bh), px(radius), cr, fit))
        # a hairline border around the whole block, as x.com draws
        ops.append(("frame", px(x), px(y), px(w), px(h), px(radius), corners, T["line"]))
        return h

    def quote_block(q, x, y, w, depth):
        """A quoted post as a card; returns its height."""
        qp = 12
        top = y
        ops.append(("box", px(x), px(y), px(w), 0, px(16), T["line"], None))   # height patched below
        box_index = len(ops) - 1
        y += qp
        ix, iw = x + qp, w - 2 * qp
        # header: small avatar, name, handle · time
        ops.append(("avatar", q["avatar"], px(ix), px(y), px(20)))
        hx = ix + 20 + 4
        badge = 18 if q["verified"] else 0
        tail = f"@{q['handle']} · {tweet.relative(q['created'], now)}"
        tail_w = fonts().font("regular", 15 * S).getlength(tail) / S
        avail = iw - 24
        name_t, name_w = _line(q["name"], True, 15, T["text"], max(60, avail - badge - 4 - tail_w))
        ops.append(("lines", name_t, px(hx), px(y)))
        cx = hx + name_w
        if q["verified"]:
            ops.append(("icon", "verified", px(cx + 2), px(y + 1), px(18), T["blue"]))
            cx += badge + 2
        ops.append(("text", tail, "regular", 15, px(cx + 4), px(y), T["gray"]))
        y += 20
        # text
        if q["spans"]:
            t = Text.make(q["spans"], 15, 20, False, T["text"], T["blue"], iw, MAX_LINES_QUOTE, T["blue"])
            ops.append(("lines", t, px(ix), px(y)))
            y += t.height
        # a deeper quote, inset
        if q.get("quote") and depth > 0:
            y += 12
            y += quote_block(q["quote"], ix, y, iw, depth - 1)
        # media hangs off the bottom edge, full width
        if q["media"]:
            y += 12
            h = media_block(q["media"], x, y, w, (False, False, True, True), True)
            y += h
        else:
            y += qp
        ops[box_index] = ("box", px(x), px(top), px(w), px(y - top), px(16), T["line"], None)
        return y - top

    y = PAD
    x0, cw = PAD + AVATAR + GAP, COL - PAD - AVATAR - GAP - PAD
    ops.append(("avatar", post["avatar"], px(PAD), px(y), px(AVATAR)))
    # header line
    badge = 20 if post["verified"] else 0
    tail = f"@{post['handle']} · {tweet.relative(post['created'], now)}"
    tail_w = fonts().font("regular", 15 * S).getlength(tail) / S
    name_t, name_w = _line(post["name"], True, 15, T["text"], max(80, cw - badge - 4 - tail_w))
    ops.append(("lines", name_t, px(x0), px(y)))
    cx = x0 + name_w
    if post["verified"]:
        ops.append(("icon", "verified", px(cx + 2), px(y + 1), px(18), T["blue"]))
        cx += badge
    ops.append(("text", tail, "regular", 15, px(cx + 4), px(y), T["gray"]))
    y += 20
    if post.get("replying_to"):
        ops.append(("text", f"Replying to @{post['replying_to']}", "regular", 15, px(x0), px(y), T["gray"], T["blue"]))
        y += 20
    if post["spans"]:
        t = Text.make(post["spans"], 15, 20, False, T["text"], T["blue"], cw, max_lines, T["blue"])
        ops.append(("lines", t, px(x0), px(y)))
        y += t.height
    if post["media"]:
        y += 12
        y += media_block(post["media"], x0, y, cw, (True, True, True, True), False)
    if post.get("quote"):
        y += 12
        y += quote_block(post["quote"], x0, y, cw, 9)
    # actions
    y += 12
    if stats:
        groups = [("reply", count(post["replies"])), ("repost", count(post["reposts"])), ("like", count(post["likes"]))]
        if post.get("views") is not None:
            groups.append(("views", count(post["views"])))
        last_w = 18.75 * 2 + 20
        step = (cw - last_w - 20) / len(groups)
        for i, (k, c) in enumerate(groups):
            gx = x0 + i * step
            ops.append(("icon", k, px(gx), px(y + 6), px(18.75), T["gray"]))
            if c:
                ops.append(("text", c, "regular", 13, px(gx + 18.75 + 8), px(y + 8), T["gray"]))
        ops.append(("icon", "bookmark", px(x0 + cw - last_w), px(y + 6), px(18.75), T["gray"]))
        ops.append(("icon", "share", px(x0 + cw - 18.75), px(y + 6), px(18.75), T["gray"]))
        y += 32
    y += PAD - 4
    h = px(y)
    L.height = h + (h % 2)
    if master[0] is None and L.cells:
        L.cells[0].master = True
    return L


# ----------------------------------------------------------------- render ---

def _mask(w: int, h: int, r: int, corners: tuple) -> Image.Image:
    m = Image.new("L", (w * 2, h * 2), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * 2 - 1, h * 2 - 1), radius=r * 2, fill=255, corners=corners)
    return m.resize((w, h), Image.LANCZOS)


def _fit(im: Image.Image, w: int, h: int, fit: str, bg) -> Image.Image:
    iw, ih = im.size
    if fit == "cover":
        k = max(w / iw, h / ih)
        nw, nh = max(1, int(round(iw * k))), max(1, int(round(ih * k)))
        im = im.resize((nw, nh), Image.LANCZOS)
        l, t = (nw - w) // 2, (nh - h) // 2
        return im.crop((l, t, l + w, t + h))
    k = min(w / iw, h / ih)
    nw, nh = max(1, int(round(iw * k))), max(1, int(round(ih * k)))
    im = im.resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGBA", (w, h), bg)
    out.paste(im, ((w - nw) // 2, (h - nh) // 2), im)
    return out


def render(L: Layout, holes: bool = True) -> bytes:
    """Draw the layout; with `holes`, every video cell is left transparent."""
    T = L.theme
    im = Image.new("RGBA", (L.width, L.height), T["bg"])
    d = ImageDraw.Draw(im)
    F = fonts()
    for op in L.ops:
        k = op[0]
        if k == "box":
            _, x, y, w, h, r, line, fill = op
            d.rounded_rectangle((x, y, x + w - 1, y + h - 1), radius=r, outline=line, width=S, fill=fill)
        elif k == "frame":
            _, x, y, w, h, r, corners, line = op
            d.rounded_rectangle((x, y, x + w - 1, y + h - 1), radius=r, outline=line, width=S, corners=corners)
        elif k == "avatar":
            _, url, x, y, size = op
            src = fetch_image(url)
            circle = _mask(size, size, size // 2, (True,) * 4)
            if src is None:
                tile = Image.new("RGBA", (size, size), T["ph"])
            else:
                tile = _fit(src, size, size, "cover", T["ph"])
            im.paste(tile, (x, y), circle)
        elif k in ("image", "poster"):
            _, url, x, y, w, h, r, corners, fit = op
            src = fetch_image(url)
            tile = Image.new("RGBA", (w, h), "#000000" if k == "poster" else T["ph"]) if src is None else _fit(src, w, h, fit, "#000000")
            im.paste(tile, (x, y), _mask(w, h, r, corners))
        elif k == "icon":
            _, kind, x, y, size, color = op
            ic = icon(kind, size, color)
            im.paste(ic, (x, y), ic)
        elif k == "text":
            _, s, role, size, x, y, color = op[:7]
            pxs = size * S
            f = F.font(role, pxs)
            asc, desc = f.getmetrics()
            lh = 20 * S if size >= 15 else 16 * S
            base = y + (lh - (asc + desc)) / 2 + asc
            if len(op) > 7 and "@" in s:
                # "Replying to @x": the handle in blue
                head, at = s.split("@", 1)
                d.text((x, base), head, font=f, fill=color, anchor="ls")
                d.text((x + f.getlength(head), base), "@" + at, font=f, fill=op[7], anchor="ls")
            else:
                d.text((x, base), s, font=f, fill=color, anchor="ls")
        elif k == "lines":
            _, t, x, y = op
            lh = t.lh * S
            for i, line in enumerate(t.lines):
                cx, ly = x, y + i * lh
                for a in line:
                    if a["kind"] == "emoji":
                        e = int(round(t.size * 1.2 * S))
                        pic = emoji_image(a["s"], e)
                        if pic is not None:
                            im.paste(pic, (int(cx + S), int(ly + (lh - e) / 2)), pic)
                        cx += a["w"] * S
                    else:
                        f = F.font(a["role"], t.size * S)
                        asc, desc = f.getmetrics()
                        base = ly + (lh - (asc + desc)) / 2 + asc
                        d.text((cx, base), a["s"], font=f, fill=a["color"], anchor="ls")
                        cx += a["w"] * S
    if holes:
        for c in L.cells:
            hole = _mask(c.w, c.h, c.radius, c.corners)
            clear = Image.new("RGBA", (c.w, c.h), (0, 0, 0, 0))
            im.paste(clear, (c.x, c.y), hole)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=False, compress_level=6)
    return out.getvalue()
