#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click", "pillow"]
# ///
"""Generate GCS Usage Bot per-message Slack avatars: the app's icon (`deploy/slack/icons/gcs.png`, the
store mark from `scripts/marks/`; `deploy/slack/README.md`) with an emoji badge.

Outputs (512px PNGs):
  gcs-digest.png            — per-message avatar for a normal digest (mark + :bar_chart: badge)
  gcs-breach.png            — per-message avatar for a ceiling/spike breach (mark + :rotating_light:)

Badge technique (mark + corner emoji, no backing circle) mirrors ~/c/rac/watchy's
gen-pfp.py; color emoji need macOS's Apple Color Emoji font.
"""
from pathlib import Path

from click import argument, command, option

S = 1024          # internal working size; outputs downscale to 512
OUT = 512


def emoji_glyph(ch: str, px: int):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("/System/Library/Fonts/Apple Color Emoji.ttc", 160)
    img = Image.new("RGBA", (320, 320), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((80, 80), ch, font=font, embedded_color=True)
    return img.crop(img.getbbox()).resize((px, px), Image.LANCZOS)


def base(mark_png: Path):
    """The store mark (full-bleed, `scripts/marks/`) at the working size."""
    from PIL import Image
    return Image.open(mark_png).convert("RGBA").resize((S, S), Image.LANCZOS)


def badge(img, ch: str):
    """Composite an emoji badge into the bottom-right corner (glyph content only occludes)."""
    from PIL import Image
    out = img.copy()
    c = S - int(S * 0.30)
    g = emoji_glyph(ch, int(S * 0.52))
    out.alpha_composite(g, (c - g.width // 2, c - g.height // 2))
    return out


def save(img, path: Path):
    img.convert("RGB").resize((OUT, OUT), __import__("PIL").Image.LANCZOS).save(path)
    return path


@command()
@option("-m", "--mark", type=Path, default=Path(__file__).parent.parent / "deploy/slack/icons/gcs.png", help="Store mark PNG (full-bleed square)")
@option("-o", "--out-dir", type=Path, default=Path(__file__).parent / "icons", help="output dir")
def main(mark: Path, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    icon = base(mark)
    made = [
        save(badge(icon, "\U0001F4CA"), out_dir / "gcs-digest.png"),   # 📊
        save(badge(icon, "\U0001F6A8"), out_dir / "gcs-breach.png"),   # 🚨
    ]
    for p in made:
        print(p)


if __name__ == "__main__":
    main()
