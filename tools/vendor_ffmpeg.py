"""Fetch ffmpeg.wasm into static/vendor for the remux window, which converts
uploads in the browser so the file never reaches the server. Pinned versions,
checked against npm's sha512 before anything is unpacked. The image build runs
this; for local dev run it once: python tools/vendor_ffmpeg.py

Served at /vendor/ffmpeg-<ver>/ (the wrapper and its worker) and
/vendor/core-<ver>/ (the single-threaded core; the threaded one needs
cross-origin isolation, which the page's thumbnails from other sites would break).
"""
from __future__ import annotations

import base64
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

UA = "OpenAI File Downloader, XaiImageApiFetch/1.0"
PACKAGES = [
    ("ffmpeg", "0.12.15", "https://registry.npmjs.org/@ffmpeg/ffmpeg/-/ffmpeg-0.12.15.tgz",
     "1C8Obr4GsN3xw+/1Ww6PFM84wSQAGsdoTuTWPOj2OizsRDLT4CXTaVjPhkw6ARyDus1B9X/L2LiXHqYYsGnRFw==",
     "package/dist/esm/", (".js", ".mjs")),
    ("core", "0.12.10", "https://registry.npmjs.org/@ffmpeg/core/-/core-0.12.10.tgz",
     "dzNplnn2Nxle2c2i2rrDhqcB19q9cglCkWnoMTDN9Q9l3PvdjZWd1HfSPjCNWc/p8Q3CT+Es9fWOR0UhAeYQZA==",
     "package/dist/esm/", (".js", ".wasm")),
]


def main(out: Path) -> None:
    for name, ver, url, integrity, prefix, exts in PACKAGES:
        dest = out / f"{name}-{ver}"
        if dest.is_dir() and any(dest.iterdir()):
            print(f"{dest} already there")
            continue
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            blob = r.read()
        got = base64.b64encode(hashlib.sha512(blob).digest()).decode()
        if got != integrity:
            sys.exit(f"{url}: sha512 mismatch")
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for m in tar.getmembers():
                if not m.isfile() or not m.name.startswith(prefix) or not m.name.endswith(exts):
                    continue
                rel = m.name[len(prefix):]
                if "/" in rel:
                    continue
                (dest / rel).write_bytes(tar.extractfile(m).read())
        print(f"{dest}: {', '.join(sorted(p.name for p in dest.iterdir()))}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "static" / "vendor")
