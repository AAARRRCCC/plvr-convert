"""Draw a post the way x.com shows it in a timeline: a 598px column at 2x,
name and handle, the text with its links in blue, the media grid, the quoted
post as a card, and the action row with its counts. A Substack note (site
"substack") is laid out by notecard.py, as substack.com shows it, and drawn
here.

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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import tweet
from .resolve import MAX_REDIRECTS, assert_public, public_transport_sync

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
# the accent each site draws its links, badges and marks in, where it isn't x.com's blue
ACCENTS = {"substack": "#ff6719"}
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
        # Substack's weights between the two, and its serif for a quoted passage
        "medium": ["Inter-Medium.otf", "Inter-Medium.ttf", "seguisb.ttf", "Inter-SemiBold.otf", "Inter-Bold.otf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"],
        "semibold": ["Inter-SemiBold.otf", "Inter-SemiBold.ttf", "seguisb.ttf", "Inter-Bold.otf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"],
        "serif": ["NotoSerif-Regular.ttf", "georgia.ttf", "DejaVuSerif.ttf"],
        "cjk": ["NotoSansCJK-Regular.ttc", "NotoSansCJKjp-Regular.otf", "NotoSansJP-Regular.otf", "YuGothR.ttc", "msgothic.ttc", "NotoSansSC-VF.ttf"],
        "cjk-bold": ["NotoSansCJK-Bold.ttc", "NotoSansCJKjp-Bold.otf", "NotoSansJP-Bold.otf", "YuGothB.ttc", "msgothic.ttc", "NotoSansSC-VF.ttf"],
        "other": ["NotoSans-Regular.ttf", "NotoSans[wdth,wght].ttf", "segoeui.ttf", "DejaVuSans.ttf"],
        "other-bold": ["NotoSans-Bold.ttf", "NotoSans[wdth,wght].ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"],
        "emoji": ["NotoColorEmoji.ttf", "seguiemj.ttf"],
        "mono": ["NotoSansMono-Regular.ttf", "NotoMono-Regular.ttf", "DejaVuSansMono.ttf", "consola.ttf"],
        "mono-bold": ["NotoSansMono-Bold.ttf", "DejaVuSansMono-Bold.ttf", "consolab.ttf"],
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

    def role_for(self, ch: str, bold: bool | str) -> str:
        """The face a character is drawn in; `bold` is True, False or the
        face wanted (medium, semibold, serif), the others its fallbacks."""
        cp = ord(ch)
        base = face(bold)
        if cp < 0x2000 or cp in self.cmap(base):
            return base
        bold = base in ("bold", "medium", "semibold")
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
                            f.set_variation_by_axes([WEIGHTS.get(role, 700 if "bold" in role else 400)])
                        except Exception:
                            pass
                except OSError:
                    f = ImageFont.load_default(size=int(round(px)))
                self._fonts[key] = f
        return f


_fonts: Fonts | None = None
WEIGHTS = {"medium": 500, "semibold": 600}


def face(bold: bool | str) -> str:
    """The face a Text is set in: bold or regular, or one named."""
    return bold if isinstance(bold, str) else "bold" if bold else "regular"


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
IMG_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")    # what x.com, twemoji and the link cards serve; no other decoder sees fetched bytes


def fetch_bytes(url: str, timeout: float = 12) -> bytes | None:
    with _img_lock:
        hit = _img_cache.get(url)
        if hit and time.time() - hit[0] < IMG_TTL:
            return hit[1]
    data = None
    try:
        assert_public(url)
        # read up to the cap and no further; identity, so a compressed body can't inflate far past it
        with httpx.Client(headers={"User-Agent": UA, "Accept-Encoding": "identity"}, transport=public_transport_sync(),
                          follow_redirects=True, max_redirects=MAX_REDIRECTS, timeout=timeout) as c, c.stream("GET", url) as r:
            size = r.headers.get("content-length", "")
            if r.status_code < 400 and not (size.isdigit() and int(size) > IMG_MAX):
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > IMG_MAX:
                        break
                else:
                    data = bytes(buf)
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
        im = Image.open(io.BytesIO(b), formats=IMG_FORMATS)
        im.load()
        return im.convert("RGBA")
    except Exception:
        return None


def prefetch(layouts: list[Layout]) -> None:
    """Fetch every image the layouts draw, several at once, into the cache
    render reads from: drawing fetches them one after another."""
    urls = set()
    for L in layouts:
        for op in L.ops:
            if op[0] in ("avatar", "image", "poster", "postcard") and op[1]:
                urls.add(op[1])
            elif op[0] == "lines":
                urls |= {TWEMOJI.format(code=twemoji_code(a["s"])) for line in op[1].lines for a in line if a["kind"] == "emoji"}
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(fetch_bytes, urls))


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


# Substack's glyphs, drawn as outlines: (path, its box, stroke width in that
# box). The action row is Substack's own, 1.5 wide in a 20 box; the rest are
# Lucide's (ISC licensed), as Substack uses them.
STROKES = {
    "s_like": ("M5.00002 2.54822C8.00003 2.09722 9.58337 4.93428 10 5.87387C10.4167 4.93428 12 2.09722 15 2.54822C18 2.99923 18.75 5.66154 "
               "18.75 7.05826C18.75 9.28572 18.1249 10.9821 16.2499 13.244C14.3749 15.506 10 18.3333 10 18.3333C10 18.3333 5.62498 15.506 "
               "3.74999 13.244C1.875 10.9821 1.25 9.28572 1.25 7.05826C1.25 5.66154 2 2.99923 5.00002 2.54822Z", 20, 1.5),
    "s_reply": ("M18.7502 11V7.50097C18.7502 4.73917 16.5131 2.50033 13.7513 2.50042L6.25021 2.50044C3.48848 2.5004 1.25017 4.73875 "
                "1.2502 7.50048L1.25021 10.9971C1.2502 13.749 3.47395 15.9836 6.22586 15.9971L6.82888 16V19.0182L12.1067 16H13.7502C16.5116 "
                "16 18.7502 13.7614 18.7502 11Z", 20, 1.5),
    "s_restack": ("M2.53001 7.81595C3.49179 4.73911 6.43281 2.5 9.91173 2.5C13.1684 2.5 15.9537 4.46214 17.0852 7.23684L17.6179 8.67647"
                  "M17.6179 8.67647L18.5002 4.26471M17.6179 8.67647L13.6473 6.91176M17.4995 12.1841C16.5378 15.2609 13.5967 17.5 10.1178 "
                  "17.5C6.86118 17.5 4.07589 15.5379 2.94432 12.7632L2.41165 11.3235M2.41165 11.3235L1.5293 15.7353M2.41165 11.3235L6.38224 "
                  "13.0882", 20, 1.5),
    "s_share": ("M10.2171 2.2793L10.2171 12.9745M10.2171 2.2793L13.333 4.99984M10.2171 2.2793L7.08301 4.99984M2.49967 10.9925L2.49967 "
                "14.1592C2.49967 16.011 4.00084 17.5121 5.85261 17.5121L14.9801 17.5121C16.8318 17.5121 18.333 16.011 18.333 14.1592L18.333 "
                "10.9925", 20, 1.5),
    "s_save": ("m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z", 24, 2),
    "s_more": ("M4 12a1 1 0 1 0 2 0a1 1 0 1 0-2 0M11 12a1 1 0 1 0 2 0a1 1 0 1 0-2 0M18 12a1 1 0 1 0 2 0a1 1 0 1 0-2 0", 24, 2),
    "s_next": ("m9 18 6-6-6-6", 24, 2),
}
# filled glyphs drawn in a box other than ICONS' 24, top-aligned in it as
# Substack draws them: the opening quote mark over a quoted passage
SHAPES = {
    "s_quote": ("M11.7188 10.6172C11.7188 7.5 9.41016 5.01562 6.43359 5.01562C5.03906 5.01562 3.71484 5.56641 2.74219 6.64453H2.42578C3.10547 "
                "4.42969 5.16797 2.57812 7.73438 1.81641C8.09766 1.69922 8.36719 1.59375 8.53125 1.45312C8.71875 1.30078 8.82422 1.11328 "
                "8.82422 0.84375C8.82422 0.351562 8.44922 0.0117188 7.89844 0.0117188C7.53516 0.0117188 7.25391 0.0820312 6.75 0.246094C5.13281 "
                "0.773438 3.67969 1.69922 2.55469 2.88281C0.972656 4.52344 0 6.71484 0 9.31641C0 13.7109 2.78906 16.2539 6.05859 16.2539C9.28125 "
                "16.2539 11.7188 13.793 11.7188 10.6172ZM25.793 10.6172C25.793 7.5 23.4727 5.01562 20.5078 5.01562C19.1133 5.01562 17.7773 "
                "5.56641 16.8164 6.64453H16.5C17.1797 4.42969 19.2422 2.57812 21.7969 1.81641C22.1602 1.69922 22.4414 1.59375 22.6055 "
                "1.45312C22.793 1.30078 22.8867 1.11328 22.8867 0.84375C22.8867 0.351562 22.5234 0.0117188 21.9727 0.0117188C21.5977 0.0117188 "
                "21.3281 0.0820312 20.8125 0.246094C19.207 0.773438 17.7422 1.69922 16.6172 2.88281C15.0469 4.52344 14.0742 6.71484 14.0742 "
                "9.31641C14.0742 13.7109 16.8633 16.2539 20.1328 16.2539C23.3555 16.2539 25.793 13.793 25.793 10.6172Z", 27),
}
# substack.com's own colours: "card" is a quoted note, "media" what shows
# behind an image, "more" the menu dots
SUBSTACK = {
    "dark": {"bg": "#161718", "text": "#eeeeee", "gray": "#777777", "line": "#2d2e2f", "blue": ACCENTS["substack"], "ph": "#232525",
             "card": "#1b1c1d", "media": "#232525", "more": "#5a5b5b"},
    "light": {"bg": "#ffffff", "text": "#363737", "gray": "#777777", "line": "#e6e6e6", "blue": ACCENTS["substack"], "ph": "#f2f2f2",
              "card": "#ffffff", "media": "#f2f2f2", "more": "#b4b4b4"},
}


def palette(theme: str, site: str | None = None) -> dict:
    """A theme's colours, with the site's accent in place of x.com's blue.
    Substack has its own light and dark, and draws dim as dark."""
    if site == "substack":
        return SUBSTACK["light" if theme == "light" else "dark"]
    T = THEMES.get(theme, THEMES["dark"])
    return dict(T, blue=ACCENTS[site]) if site in ACCENTS else T


def icon(kind: str, px: int, color: str) -> Image.Image:
    """A filled 24-box glyph, drawn at 4x and shrunk for smooth edges."""
    ss = 4
    im = Image.new("RGBA", (px * ss, px * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    k = px * ss / 24
    if kind in STROKES:
        path, box, width = STROKES[kind]
        k = px * ss / box
        w = round(width * k)
        for poly in path_polylines(path):
            pts = [(x * k, y * k) for x, y in poly]
            d.line(pts, fill=color, width=w, joint="curve")
            for x, y in (pts[0], pts[-1]):
                d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color)
        return im.resize((px, px), Image.LANCZOS)
    if kind in SHAPES:
        path, box = SHAPES[kind]
        k = px * ss / box
        for poly in path_polylines(path):
            d.polygon([(x * k, y * k) for x, y in poly], fill=color)
        return im.resize((px, px), Image.LANCZOS)
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
                        atoms.append({"kind": "space", "s": " ", "w": F.font(face(bold), size * S).getlength(" ") / S, "color": col, "role": face(bold)})
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


GRID_MAX = 1.25            # a grid of several is at most this tall for its width
ASPECT = (1 / 3, 3)        # a stranger shape than this is letterboxed in its cell


def _groups(n: int) -> list[list[list[int]]]:
    """Every way to cut 0..n-1 into runs that keep their order."""
    out = []
    for mask in range(1 << (n - 1)):
        runs, run = [], [0]
        for i in range(1, n):
            if mask >> (i - 1) & 1:
                runs.append(run)
                run = []
            run.append(i)
        out.append(runs + [run])
    return out


def _rows(a: list[float], runs: list[list[int]], w: float, gap: float) -> tuple[list[tuple], float]:
    boxes, y = [], 0.0
    for run in runs:
        h = (w - gap * (len(run) - 1)) / sum(a[i] for i in run)
        x = 0.0
        for i in run:
            boxes.append((i, x, y, a[i] * h, h))
            x += a[i] * h + gap
        y += h + gap
    return boxes, y - gap


def _cols(a: list[float], runs: list[list[int]], w: float, gap: float) -> tuple[list[tuple], float]:
    # every column is the same height H, so each width is (H - its gaps) / sum(h/w)
    r = [sum(1 / a[i] for i in run) for run in runs]
    H = (w - gap * (len(runs) - 1) + sum(gap * (len(run) - 1) / rc for run, rc in zip(runs, r))) / sum(1 / rc for rc in r)
    boxes, x = [], 0.0
    for run, rc in zip(runs, r):
        cw = (H - gap * (len(run) - 1)) / rc
        y = 0.0
        for i in run:
            boxes.append((i, x, y, cw, cw / a[i]))
            y += cw / a[i] + gap
        x += cw + gap
    return boxes, H


def media_boxes(items: list[dict], x: float, y: float, w: float, corners: tuple) -> tuple[list[tuple], float]:
    """Where each media item goes, in css px, and the block's height. Several
    are each shown whole: of every arrangement in rows or in columns that keeps
    their order and fits the column's width under GRID_MAX, the one whose
    smallest item is largest. Only when none fits is one shrunk to centre,
    which leaves the width beside it empty."""
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
    raw = [(it.get("w") or 16) / (it.get("h") or 9) for it in items]
    a = [min(max(v, ASPECT[0]), ASPECT[1]) for v in raw]
    best, best_score = None, (False, -1.0)
    for runs in _groups(n):
        for arrange in (_rows, _cols):
            boxes, h = arrange(a, runs, w, gap)
            k = min(1.0, w * GRID_MAX / h)
            # one that fits beats any that is shrunk; then the smallest item,
            # as large as it can be, less the width left empty
            score = (k == 1.0, min(bw * bh for _, _, _, bw, bh in boxes) * k * k * k)
            if score > (best_score[0], best_score[1] + 1e-6):
                best, best_score = (boxes, h, k), score
    boxes, h, k = best
    left = x + w * (1 - k) / 2
    out = [(items[i], left + bx * k, y + by * k, bw * k, bh * k, "cover" if a[i] == raw[i] else "contain")
           for i, bx, by, bw, bh in boxes]
    return out, h * k


def layout(post: dict, theme: str = "dark", stats: bool = True, now: float | None = None, max_lines: int = MAX_LINES,
           media_only: bool = False, feed: bool = False) -> Layout:
    """With `media_only`, the media of the post and each quote it shows,
    stacked edge to edge with nothing else; a post without media is drawn whole.
    A Substack note is drawn as on its own page, or with `feed` as in a list
    of notes."""
    T = palette(theme, post.get("site"))
    L = Layout(COL * S, 0, T)
    ops = L.ops

    def px(v):
        return int(round(v * S))

    def media_block(items, x, y, w, corners, in_quote, bare=False):
        boxes, h = media_boxes(items, x, y, w, corners)
        radius = 0 if bare else 16
        # a grid shrunk to fit is narrower than the column, and framed as drawn
        x0 = min(b[1] for b in boxes)
        w = max(b[1] + b[3] for b in boxes) - x0
        x = x0
        for it, bx, by, bw, bh, fit in boxes:
            single = len(boxes) == 1
            if single:
                cr = corners
            else:
                # per-cell rounding: only the outer corners of the grid
                top, bottom, l, r = by <= y + 0.5, by + bh >= y + h - 0.5, bx <= x + 0.5, bx + bw >= x + w - 0.5
                cr = (l and top and corners[0], r and top and corners[1], r and bottom and corners[2], l and bottom and corners[3])
            if it["kind"] == "photo":
                ops.append(("image", it["url"], px(bx), px(by), px(bw), px(bh), px(radius), cr, fit))
            else:
                # a still is its poster alone, with no cell for ffmpeg to fill
                if not it.get("still"):
                    L.cells.append(Cell(px(bx), px(by), px(bw), px(bh), fit, it, px(radius), cr))
                ops.append(("poster", it.get("poster"), px(bx), px(by), px(bw), px(bh), px(radius), cr, fit))
        # a hairline border around the whole block, as x.com draws
        if not bare:
            ops.append(("frame", px(x), px(y), px(w), px(h), px(radius), corners, T["line"]))
        return h

    chain, p = [], post
    while p:
        chain.append(p)
        p = p.get("quote")
    if media_only and any(p["media"] for p in chain):
        y = 0
        for p in chain:
            if p["media"]:
                y += 4 if y else 0
                y += media_block(p["media"], 0, y, COL, (False,) * 4, False, bare=True)
        h = px(y)
        L.height = h + (h % 2)
        return _lead_cell(L)
    if post.get("site") == "substack":
        from . import notecard
        return notecard.layout(post, theme, stats, now, max_lines, feed)

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
        if q.get("translated"):
            ops.append(("text", f"Translated from {q['translated']}", "regular", 13, px(ix), px(y + 2), T["gray"]))
            y += 20
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
    if post.get("translated"):
        ops.append(("text", f"Translated from {post['translated']}", "regular", 13, px(x0), px(y + 4), T["gray"]))
        y += 22
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
    return _lead_cell(L)


def _lead_cell(L: Layout) -> Layout:
    """Mark the cell whose video the render's length and sound come from."""
    first = tweet.lead([c.item for c in L.cells])
    for c in L.cells:
        if c.item is first:
            c.master = True
            break
    return L


# ----------------------------------------------------------------- render ---

def _mask(w: int, h: int, r: int, corners: tuple) -> Image.Image:
    m = Image.new("L", (w * 2, h * 2), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * 2 - 1, h * 2 - 1), radius=r * 2, fill=255, corners=corners)
    return m.resize((w, h), Image.LANCZOS)


def _fit(im: Image.Image, w: int, h: int, fit: str, bg) -> Image.Image:
    """`cover` fills the box and crops round the middle, `top` crops from
    the top down, anything else shows it whole."""
    iw, ih = im.size
    if fit in ("cover", "top"):
        k = max(w / iw, h / ih)
        nw, nh = max(1, int(round(iw * k))), max(1, int(round(ih * k)))
        im = im.resize((nw, nh), Image.LANCZOS)
        l, t = (nw - w) // 2, 0 if fit == "top" else (nh - h) // 2
        return im.crop((l, t, l + w, t + h))
    k = min(w / iw, h / ih)
    nw, nh = max(1, int(round(iw * k))), max(1, int(round(ih * k)))
    im = im.resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGBA", (w, h), bg)
    out.paste(im, ((w - nw) // 2, (h - nh) // 2), im)
    return out


# how much of an article's cover shows down its card: whole to 30%, gone by 80%
FADE = [(0.30, 1.0), (0.41, 0.95), (0.47, 0.87), (0.53, 0.75), (0.59, 0.6), (0.65, 0.42), (0.71, 0.22), (0.80, 0.0)]


def _postcard(src: Image.Image | None, w: int, h: int, ih: int, tint: tuple | None) -> Image.Image:
    """An article card's ground as Substack draws it: the cover across the
    top fading into itself blurred, under a wash of the cover's muted colour,
    and darkening towards the bottom for the text over it."""
    tint = tuple(tint or (35, 37, 37))
    out = Image.new("RGBA", (w, h), tint + (255,))
    if src is not None:
        # blurred at an eighth of the size: the same look, and far quicker
        k = 8
        back = _fit(src, max(1, round(w * 1.3 / k)), max(1, round(h * 1.3 / k)), "cover", "#000000")
        back = back.filter(ImageFilter.GaussianBlur(32 * S / k)).resize((round(w * 1.3), round(h * 1.3)), Image.BILINEAR)
        l, t = (back.width - w) // 2, (back.height - h) // 2
        out.alpha_composite(back.crop((l, t, l + w, t + h)))
        out.alpha_composite(Image.new("RGBA", (w, h), tint + (204,)))
        if ih:
            cover = _fit(src, w, ih, "top", "#000000")
            alpha = Image.new("L", (1, ih))
            for v in range(ih):
                alpha.putpixel((0, v), round(255 * _faded(v / ih)))
            cover.putalpha(alpha.resize((w, ih)))
            out.alpha_composite(cover)
    shade = Image.new("L", (1, h))
    for v in range(h):
        shade.putpixel((0, v), round(255 * 0.3 * v / max(1, h - 1)))
    out.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (w, h), (0, 0, 0)).split(), shade.resize((w, h)))))
    return out


def _faded(t: float) -> float:
    """How much of the cover shows `t` of the way down it."""
    if t <= FADE[0][0]:
        return 1.0
    for (p0, a0), (p1, a1) in zip(FADE, FADE[1:]):
        if t <= p1:
            return a0 + (a1 - a0) * (t - p0) / (p1 - p0)
    return 0.0


def render(L: Layout, holes: bool = True, size: tuple[int, int] | None = None) -> bytes:
    """Draw the layout as a png; with `holes`, every video cell is left
    transparent. With `size` it is drawn at full size and scaled down, so the
    text keeps its shapes."""
    im = draw(L, holes)
    if size and size != im.size:
        im = im.resize(size, Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=False, compress_level=6)
    return out.getvalue()


def draw(L: Layout, holes: bool = True) -> Image.Image:
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
            # a ninth field is the width left showing, where a row of them is cut off
            _, url, x, y, w, h, r, corners, fit = op[:9]
            src = fetch_image(url)
            tile = Image.new("RGBA", (w, h), "#000000" if k == "poster" else T["ph"]) if src is None else _fit(src, w, h, fit, "#000000")
            if len(op) > 9 and op[9] < w:
                w = op[9]
                tile = tile.crop((0, 0, w, h))
            im.paste(tile, (x, y), _mask(w, h, r, corners))
        elif k == "fill":
            # a shape that lets what is under it through: rgba
            _, x, y, w, h, r, color = op
            layer = Image.new("RGBA", (w, h), color[:3] + (0,))
            layer.putalpha(_mask(w, h, r, (True,) * 4).point(lambda v: v * color[3] // 255))
            im.alpha_composite(layer, (x, y))
        elif k == "postcard":
            _, url, x, y, w, h, ih, r, tint = op
            im.paste(_postcard(fetch_image(url), w, h, ih, tint), (x, y), _mask(w, h, r, (True,) * 4))
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
    return im
