"""Generate the soft, brand-colored watercolor texture used by the QML UI."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

WIDTH, HEIGHT = 1920, 1200
PALETTE = (
    ("#43d2c1", -0.08, 0.18, 0.43, 0.44, 0.27),
    ("#e4ec8c", 0.94, 0.08, 0.42, 0.37, 0.26),
    ("#9bdbea", 1.08, 0.53, 0.35, 0.46, 0.19),
    ("#b8e88e", 0.09, 0.91, 0.40, 0.42, 0.25),
    ("#e9e59c", 0.47, 1.08, 0.34, 0.31, 0.20),
    ("#96bbef", 0.98, 0.95, 0.38, 0.45, 0.22),
    ("#b5eade", 0.49, 0.50, 0.29, 0.36, 0.09),
)


def hex_rgb(value: str) -> np.ndarray:
    return np.asarray(tuple(bytes.fromhex(value.removeprefix("#"))), dtype=np.float32)


def multiscale_pigment(seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    field = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
    detail = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
    total_weight = 0.0
    detail_weight = 0.0
    for columns, rows, weight in ((7, 5, 0.4), (18, 12, 0.3), (48, 31, 0.2), (128, 80, 0.1)):
        small = rng.random((rows, columns), dtype=np.float32)
        layer = np.asarray(
            Image.fromarray(np.uint8(small * 255)).resize(
                (WIDTH, HEIGHT), Image.Resampling.BICUBIC
            ),
            dtype=np.float32,
        ) / 255.0
        field += layer * weight
        total_weight += weight
        if columns >= 48:
            detail += layer * weight
            detail_weight += weight
    return field / total_weight, detail / detail_weight


def make_texture() -> Image.Image:
    x = np.linspace(0.0, 1.0, WIDTH, dtype=np.float32)[None, :]
    y = np.linspace(0.0, 1.0, HEIGHT, dtype=np.float32)[:, None]
    pigment, detail = multiscale_pigment(seed=26019)
    paper = np.full((HEIGHT, WIDTH, 3), (249.0, 253.0, 253.0), dtype=np.float32)

    for color_hex, cx, cy, rx, ry, strength in PALETTE:
        distance = np.sqrt(((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2)
        uneven_edge = (pigment - 0.5) * 0.58 + (detail - 0.5) * 0.14
        shape = np.clip((1.03 - distance + uneven_edge) / 0.24, 0.0, 1.0)
        shape = shape * shape * (3.0 - 2.0 * shape)
        paper_grain = 0.72 + pigment * 0.56 + (detail - 0.5) * 0.16
        alpha = np.clip(shape * strength * paper_grain, 0.0, 0.52)[..., None]
        color = hex_rgb(color_hex)
        paper = paper * (1.0 - alpha) + color * alpha

        # A faint, uneven pigment pool at the outside edge gives the wash a paper-painted feel.
        rim_distance = np.abs(distance - 0.94 - uneven_edge * 0.55)
        rim = (np.exp(-((rim_distance / 0.035) ** 2)) * shape * strength * 0.09)[..., None]
        paper = paper * (1.0 - rim) + color * rim

    fine_grain, paper_grain = multiscale_pigment(seed=26020)
    paper += (fine_grain - 0.5)[..., None] * 2.2
    paper += (paper_grain - 0.5)[..., None] * 1.1
    image = Image.fromarray(np.uint8(np.clip(paper, 0, 255)), mode="RGB")
    return image.filter(ImageFilter.GaussianBlur(radius=0.7))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            Path(__file__).resolve().parents[1]
            / "gigaforge" / "assets" / "watercolor-wash-v3.png"
        ),
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    make_texture().save(args.output, optimize=True)
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
