#!/usr/bin/env python
"""Draw the app icon and build icon.icns.

    ../../voice-studio/.venv/bin/python build/make-icon.py

A waveform in the studio's own green on its own near-black, in the rounded
square macOS expects. Nothing here is loaded at runtime — the .icns it writes
is what ships, so this only needs running when the icon changes."""
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
S = 1024                      # master size; every other size is downsampled
SS = 4                        # supersample factor, for clean curves

BG = (11, 13, 12)             # --bg
PANEL = (23, 29, 27)          # --chrome
GREEN = (127, 216, 143)       # --green
AMBER = (232, 180, 92)        # --amber


def squircle(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def main():
    n = S * SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # macOS leaves the outer ~10% as breathing room, and its rounded-square
    # mask is close to a 22% corner radius.
    pad = int(n * 0.085)
    box = (pad, pad, n - pad, n - pad)
    squircle(d, box, int(n * 0.205), BG)
    # a hairline lift so the icon does not vanish on a black dock
    d.rounded_rectangle(box, radius=int(n * 0.205), outline=PANEL,
                        width=int(n * 0.008))

    # The waveform: bars whose heights are a decaying speech-like envelope,
    # mirrored about the centre line so it reads as audio rather than a chart.
    bars = 13
    inner = n - 2 * pad
    span = inner * 0.62
    left = (n - span) / 2
    gap = span / (bars * 2 - 1)
    w = gap
    mid = n / 2
    tallest = inner * 0.30

    env = [0.30, 0.52, 0.86, 0.62, 1.00, 0.74, 0.44,
           0.80, 1.00, 0.58, 0.88, 0.46, 0.28]
    for i in range(bars):
        h = tallest * env[i]
        # a gentle taper towards the edges so the block reads as one shape
        h *= 0.55 + 0.45 * math.sin(math.pi * (i + 0.5) / bars)
        x0 = left + i * 2 * gap
        colour = AMBER if i == 8 else GREEN
        d.rounded_rectangle((x0, mid - h, x0 + w, mid + h),
                            radius=w / 2, fill=colour)

    img = img.resize((S, S), Image.LANCZOS)
    png = HERE / "icon.png"
    img.save(png)

    # .icns wants a folder of named sizes; iconutil turns it into the file.
    iconset = HERE / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    for size in (16, 32, 64, 128, 256, 512):
        img.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
        img.resize((size * 2, size * 2), Image.LANCZOS).save(
            iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset),
                    "-o", str(HERE / "icon.icns")], check=True)
    print("wrote", HERE / "icon.icns")


if __name__ == "__main__":
    main()
