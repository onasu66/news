#!/usr/bin/env python3
"""カテゴリ別 OGP / カード画像を生成する（1回実行でOK）。"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "app" / "static" / "og"

CATEGORIES = [
    ("tech", "テクノロジー", (30, 64, 175), (99, 102, 241)),
    ("domestic", "国内ニュース", (5, 120, 90), (16, 185, 129)),
    ("international", "国際ニュース", (180, 83, 9), (245, 158, 11)),
    ("politics", "政治・社会", (153, 27, 27), (220, 38, 38)),
    ("research", "研究・論文", (88, 28, 135), (168, 85, 247)),
    ("entertainment", "エンタメ", (190, 24, 93), (236, 72, 153)),
    ("sports", "スポーツ", (21, 128, 61), (34, 197, 94)),
    ("default", "知リポAI", (49, 46, 129), (129, 140, 248)),
]

FONT_CANDIDATES = [
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "C:/Windows/Fonts/YuGothM.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for fp in FONT_CANDIDATES:
        p = Path(fp)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size=size, index=0)
            except Exception:
                try:
                    return ImageFont.truetype(str(p), size=size)
                except Exception:
                    pass
    return ImageFont.load_default()


def _gradient(size: tuple[int, int], c1: tuple[int, int, int], c2: tuple[int, int, int]) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(c1[0] + (c2[0] - c1[0]) * t)
        g = int(c1[1] + (c2[1] - c1[1]) * t)
        b = int(c1[2] + (c2[2] - c1[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return img


def _draw_brand(img: Image.Image, label: str, subtitle: str) -> None:
    draw = ImageDraw.Draw(img)
    w, h = img.size
    # 半透明オーバーレイ
    overlay = Image.new("RGBA", img.size, (15, 23, 42, 80))
    img_rgba = img.convert("RGBA")
    img_rgba = Image.alpha_composite(img_rgba, overlay)
    img.paste(img_rgba.convert("RGB"))

    title_font = _load_font(max(28, h // 14))
    sub_font = _load_font(max(16, h // 28))
    brand_font = _load_font(max(14, h // 36))

    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([(w * 0.06, h * 0.28), (w * 0.94, h * 0.72)], radius=24, fill=(255, 255, 255, 18), outline=(255, 255, 255, 60), width=2)

    draw.text((w * 0.1, h * 0.34), label, fill=(255, 255, 255), font=title_font)
    draw.text((w * 0.1, h * 0.52), subtitle, fill=(226, 232, 240), font=sub_font)
    draw.text((w * 0.1, h * 0.82), "知リポAI — 偉人AIの多角的な視点で読む", fill=(203, 213, 225), font=brand_font)


def generate_one(slug: str, label: str, c1: tuple, c2: tuple, w: int, h: int) -> Path:
    img = _gradient((w, h), c1, c2)
    sub = "AIが背景とポイントを解説"
    if slug == "research":
        sub = "最新論文をわかりやすく"
    elif slug == "default":
        sub = "ニュースと論文をAIが解説"
    _draw_brand(img, label, sub)
    out = OUT_DIR / f"{'og' if w >= 800 else 'card'}-{slug}.jpg"
    img.save(out, "JPEG", quality=88, optimize=True)
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for slug, label, c1, c2 in CATEGORIES:
        og = generate_one(slug, label, c1, c2, 1200, 630)
        card = generate_one(slug, label, c1, c2, 400, 225)
        print("OK", og.name, card.name)
    print(f"Generated {len(CATEGORIES) * 2} images in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
