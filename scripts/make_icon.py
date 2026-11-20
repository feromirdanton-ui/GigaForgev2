"""Generate the GigaForge application icon (.ico) with Pillow.

The window and any shortcut need an icon, and shipping a binary nobody can
regenerate is worse than generating it: run this script and it rewrites
``assets/gigaforge.ico`` from the same palette the UI uses, at every size Windows
asks for (16, 24, 32, 48, 64, 128, 256).

    python scripts/make_icon.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Keep in sync with gigaforge/desktop/theme.py (Palette)
BG = "#0e141c"
PANEL = "#131b25"
ACCENT = "#2fe0c4"
VIOLET = "#8b5cff"
TEXT = "#e6eef8"

SIZES = (16, 24, 32, 48, 64, 128, 256)


def build(size: int) -> Image.Image:
    """A rounded dark tile with a teal 'F' and a violet forging spark."""
    scale = 8  # supersample, then downscale for clean edges
    canvas = size * scale
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    radius = int(canvas * 0.22)
    draw.rounded_rectangle(
        [0, 0, canvas - 1, canvas - 1], radius=radius, fill=BG, outline=ACCENT, width=scale
    )
    # inner panel
    inset = int(canvas * 0.10)
    draw.rounded_rectangle(
        [inset, inset, canvas - 1 - inset, canvas - 1 - inset],
        radius=int(radius * 0.72),
        fill=PANEL,
    )

    # the spark: a soft diagonal behind the monogram, so the letter stays legible
    spark = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    spark_draw = ImageDraw.Draw(spark)
    spark_draw.line(
        [(int(canvas * 0.26), int(canvas * 0.80)), (int(canvas * 0.74), int(canvas * 0.22))],
        fill=VIOLET,
        width=max(scale * 3, int(canvas * 0.10)),
    )
    spark = spark.filter(ImageFilter.GaussianBlur(radius=canvas * 0.02))
    spark.putalpha(spark.getchannel("A").point(lambda a: int(a * 0.55)))
    img.alpha_composite(spark)

    text = "F"
    font = None
    for candidate in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(candidate, int(canvas * 0.56))
            break
        except OSError:
            continue
    if font is None:  # pragma: no cover - minimal font set
        font = ImageFont.load_default()
        draw.text((canvas * 0.30, canvas * 0.24), text, fill=TEXT, font=font)
    else:
        box = draw.textbbox((0, 0), text, font=font)
        x = (canvas - (box[2] - box[0])) / 2 - box[0]
        y = (canvas - (box[3] - box[1])) / 2 - box[1]
        draw.text((x, y), text, fill=TEXT, font=font)

    return img.resize((size, size), Image.LANCZOS)


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "assets" / "gigaforge.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    images = [build(size) for size in SIZES]
    images[-1].save(target, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    png = target.with_suffix(".png")
    images[-1].save(png, format="PNG")
    print(f"wrote {target} ({target.stat().st_size:,} bytes) and {png.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

