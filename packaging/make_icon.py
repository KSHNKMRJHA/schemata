"""Render the Schemata brand mark as a Windows .ico (dark tile + brass schematic chip)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

_BRASS = (217, 164, 65, 255)
_BRASS_DIM = (217, 164, 65, 160)
_BG = (16, 23, 32, 255)
_BG_LINE = (30, 43, 58, 255)
_DARK = (10, 13, 18, 255)


def _size(canvas: int, p: float) -> int:
    return max(1, round(canvas * p))


def _draw(canvas: int) -> Image.Image:
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    tile = int(canvas * 0.90)
    off = (canvas - tile) // 2
    d.rounded_rectangle([off, off, off + tile, off + tile], radius=_size(canvas, 0.18), fill=_BG)
    d.rounded_rectangle(
        [off, off, off + tile, off + tile],
        radius=_size(canvas, 0.18),
        outline=_BRASS_DIM,
        width=max(1, _size(canvas, 0.015)),
    )

    b = off + _size(canvas, 0.16)
    e = off + tile - _size(canvas, 0.16)

    chip_w = int((e - b) * 0.56)
    chip_h = int((e - b) * 0.56)
    cx = (b + e) // 2
    cy = (b + e) // 2
    c0 = (cx - chip_w // 2, cy - chip_h // 2)
    c1 = (cx + chip_w // 2, cy + chip_h // 2)

    pins = 6
    pin_span = int(chip_w * 0.9)
    gap = int((e - b) * 0.14)
    top_lead = c0[1] - gap
    bot_lead = c1[1] + gap
    pin_len = int(gap * 0.55)
    for i in range(pins):
        x = cx - pin_span // 2 + i * (pin_span // (pins - 1))
        w = max(1, _size(canvas, 0.018))
        d.line([x, top_lead, x, top_lead + pin_len], fill=_BRASS, width=w)
        d.line([x, bot_lead - pin_len, x, bot_lead], fill=_BRASS, width=w)

    d.rounded_rectangle(
        [c0, c1], radius=_size(canvas, 0.04), fill=_DARK, outline=_BRASS, width=max(1, _size(canvas, 0.02))
    )

    bar_w = int(chip_w * 0.36)
    bar_h = max(1, _size(canvas, 0.02))
    d.rectangle([cx - bar_w // 2, cy - bar_h // 2, cx + bar_w // 2, cy + bar_h // 2], fill=_BRASS)

    node_r = max(2, _size(canvas, 0.022))
    d.ellipse([cx - node_r, top_lead - node_r, cx + node_r, top_lead + node_r], fill=_BRASS)

    grid_n = 2
    step = (e - b) // (grid_n + 1)
    for gx in range(1, grid_n + 1):
        d.line([b, b + gx * step, e, b + gx * step], fill=_BG_LINE, width=1)
        d.line([b + gx * step, b, b + gx * step, e], fill=_BG_LINE, width=1)
    return img


def main() -> None:
    out = Path(__file__).resolve().parent / "icon.ico"
    img = _draw(512)
    img.save(out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"icon written: {out}")


if __name__ == "__main__":
    main()
