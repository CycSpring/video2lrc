"""Generate the deterministic Video2LRC application icon assets."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
WORK = ROOT / "work"
MASTER_SIZE = 1024
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

INK = "#17252B"
MINT = "#3ED6C2"
CORAL = "#FF6B5B"
IVORY = "#F7FAF8"


def render_icon(size: int = MASTER_SIZE) -> Image.Image:
    """Render a music-note and spaced-caption mark at the requested size."""

    scale = size / MASTER_SIZE

    def box(values: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return tuple(round(value * scale) for value in values)

    def width(value: int) -> int:
        return max(1, round(value * scale))

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle(box((64, 64, 960, 960)), radius=width(210), fill=INK)
    draw.rounded_rectangle(
        box((91, 91, 933, 933)),
        radius=width(181),
        outline=(255, 255, 255, 34),
        width=width(12),
    )

    # A connected pair of notes remains legible at Windows title-bar sizes.
    draw.polygon(
        [
            (round(365 * scale), round(262 * scale)),
            (round(700 * scale), round(198 * scale)),
            (round(700 * scale), round(300 * scale)),
            (round(365 * scale), round(364 * scale)),
        ],
        fill=MINT,
    )
    draw.rounded_rectangle(box((365, 270, 445, 620)), radius=width(34), fill=MINT)
    draw.rounded_rectangle(box((620, 220, 700, 568)), radius=width(34), fill=MINT)
    draw.ellipse(box((286, 548, 456, 688)), fill=MINT)
    draw.ellipse(box((541, 496, 711, 636)), fill=MINT)

    # The separated lyric strokes deliberately echo word-level visual spacing.
    draw.rounded_rectangle(box((196, 718, 828, 846)), radius=width(52), fill=CORAL)
    for segment in ((264, 768, 405, 796), (444, 768, 575, 796), (614, 768, 760, 796)):
        draw.rounded_rectangle(box(segment), radius=width(14), fill=IVORY)

    return image


def build_preview(images: dict[int, Image.Image]) -> Image.Image:
    """Create a visual QA sheet showing every important Windows icon size."""

    sizes = (16, 24, 32, 48, 64, 128, 256)
    margin = 34
    gap = 22
    card_widths = [max(132, icon_size + 48) for icon_size in sizes]
    preview_width = margin * 2 + sum(card_widths) + gap * (len(sizes) - 1)
    preview = Image.new("RGB", (preview_width, 640), "#E9EDF0")
    draw = ImageDraw.Draw(preview)
    x = margin
    for icon_size, card_width in zip(sizes, card_widths):
        card = (x, 42, x + card_width, 598)
        draw.rounded_rectangle(card, radius=12, fill="#FFFFFF", outline="#C6CED3", width=2)
        draw.text((x + 16, 60), f"{icon_size} x {icon_size}", fill="#17252B")
        icon = images[icon_size]
        icon_x = x + (card_width - icon_size) // 2
        preview.alpha_composite(icon, (icon_x, 122)) if preview.mode == "RGBA" else preview.paste(
            icon, (icon_x, 122), icon
        )
        zoom = icon.resize((96, 96), Image.Resampling.NEAREST)
        preview.paste(zoom, (x + (card_width - 96) // 2, 450), zoom)
        x += card_width + gap
    return preview


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    master = render_icon()
    png_path = ASSETS / "video2lrc-icon.png"
    ico_path = ASSETS / "video2lrc.ico"
    master.save(png_path, format="PNG", optimize=True)
    master.save(ico_path, format="ICO", sizes=[(size, size) for size in ICON_SIZES])

    preview_images = {
        size: master.resize((size, size), Image.Resampling.LANCZOS)
        for size in (16, 24, 32, 48, 64, 128, 256)
    }
    build_preview(preview_images).save(WORK / "icon-preview.png", format="PNG", optimize=True)
    print(png_path)
    print(ico_path)
    print(WORK / "icon-preview.png")


if __name__ == "__main__":
    main()
