"""Draw the integration's icon: a white fish on a green rounded square (stdlib only, deterministic).

    python scripts/make_icon.py            # writes custom_components/fishing/brand/icon.png (256x256)

Home Assistant and HACS read `brand/icon.png` from inside the integration. Drawing it from code keeps the
icon reproducible and avoids committing an image of unknown origin. Edges are anti-aliased by rendering at
4x and averaging down.
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "custom_components" / "fishing" / "brand" / "icon.png"

SIZE = 256
SS = 4                                  # supersampling factor
BG = (15, 118, 110)                     # teal green
FG = (255, 255, 255)
EYE = (15, 118, 110)


def in_rounded_square(x: float, y: float, size: float, radius: float) -> bool:
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def in_ellipse(x: float, y: float, cx: float, cy: float, rx: float, ry: float) -> bool:
    return ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0


def in_triangle(x: float, y: float, a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> bool:
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    d1, d2, d3 = sign((x, y), a, b), sign((x, y), b, c), sign((x, y), c, a)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def sample(x: float, y: float) -> tuple[int, int, int, int]:
    """Colour of one point of the 256x256 design, RGBA (transparent outside the rounded square)."""
    if not in_rounded_square(x, y, SIZE, 52):
        return (0, 0, 0, 0)
    # the fish faces left: body, tail fin, a small dorsal fin, and an eye
    fish = (
        in_ellipse(x, y, 112, 130, 80, 46)
        or in_triangle(x, y, (176, 130), (232, 86), (232, 174))
        or in_triangle(x, y, (96, 92), (140, 92), (122, 62))
    )
    if in_ellipse(x, y, 70, 118, 8, 8):
        return (*EYE, 255)
    if fish:
        return (*FG, 255)
    return (*BG, 255)


def render() -> list[bytes]:
    rows = []
    n = SS * SS
    for py in range(SIZE):
        row = bytearray()
        for px in range(SIZE):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    cr, cg, cb, ca = sample(px + (sx + 0.5) / SS, py + (sy + 0.5) / SS)
                    r, g, b, a = r + cr * ca, g + cg * ca, b + cb * ca, a + ca     # premultiplied average
            if a == 0:
                row += bytes((0, 0, 0, 0))
            else:
                row += bytes((round(r / a), round(g / a), round(b / a), round(a / n)))
        rows.append(bytes(row))
    return rows


def png(rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rows)                    # filter type 0 on every scanline
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)      # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png(render()))
    print(f"{out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
