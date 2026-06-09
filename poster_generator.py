import os
import yaml
import random
import requests
import re

from io import BytesIO
from PIL import (
    Image,
    ImageDraw,
    ImageFont,
    ImageFilter
)

# --------------------------------------------------
# Config Loading
# --------------------------------------------------

with open(
    "config/poster_fonts.yaml",
    "r",
    encoding="utf-8"
) as f:
    POSTER_FONTS = yaml.safe_load(f)

with open(
    "config/poster_theme.yaml",
    "r",
    encoding="utf-8"
) as f:
    POSTER_THEME = yaml.safe_load(f)

with open(
    "config/poster_layout.yaml",
    "r",
    encoding="utf-8"
) as f:
    POSTER_LAYOUT = yaml.safe_load(f)


# --------------------------------------------------
# Image Helpers
# --------------------------------------------------

def load_image(source):

    if source.startswith("http"):

        response = requests.get(
            source,
            timeout=30
        )

        response.raise_for_status()

        return Image.open(
            BytesIO(response.content)
        )

    return Image.open(source)


def scale_to_fill(
    img,
    target_width,
    target_height
):

    scale = max(
        target_width / img.width,
        target_height / img.height
    )

    return img.resize(
        (
            int(img.width * scale),
            int(img.height * scale)
        ),
        Image.LANCZOS
    )


def crop_to_canvas(
    img,
    width,
    height,
    offset_x=None,
    offset_y=None
):
    left = (img.width - width) // 2

    top = (img.height - height) // 2

    if offset_x is None:
        offset_x = POSTER_LAYOUT["canvas"].get(
            "image_offset_x",
            0
        )

    if offset_y is None:
        offset_y = POSTER_LAYOUT["canvas"].get(
            "image_offset_y",
            0
        )

    left += int(
        (img.width - width)
        * offset_x / 100
    )

    top += int(
        (img.height - height)
        * offset_y / 100
    )

    left = max(
        0,
        min(
            left,
            img.width - width
        )
    )

    top = max(
        0,
        min(
            top,
            img.height - height
        )
    )

    return img.crop(
        (
            left,
            top,
            left + width,
            top + height
        )
    )


def prepare_background(img, offset_x=None, offset_y=None):

    canvas_cfg = POSTER_LAYOUT["canvas"]

    width = canvas_cfg["width"]
    height = canvas_cfg["height"]

    img = img.convert("RGBA")

    img = scale_to_fill(
        img,
        width,
        height
    )

    img = crop_to_canvas(
        img,
        width,
        height,
        offset_x,
        offset_y
    )

    img = img.filter(
        ImageFilter.GaussianBlur(
            radius=POSTER_THEME["background"]["blur_radius"]
        )
    )

    opacity = int(
        255 *
        POSTER_THEME["background"]["darken_opacity"]
    )

    overlay = Image.new(
        "RGBA",
        img.size,
        (0, 0, 0, opacity)
    )

    img.alpha_composite(overlay)

    return img

def get_fallback_art(system):

    system_dir = os.path.join(
        "art",
        system
    )

    generic_dir = os.path.join(
        "art",
        "generic"
    )

    candidates = []

    if os.path.isdir(system_dir):

        candidates = [
            os.path.join(system_dir, f)
            for f in os.listdir(system_dir)
            if f.lower().endswith(
                (
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp"
                )
            )
        ]

    if not candidates:

        candidates = [
            os.path.join(generic_dir, f)
            for f in os.listdir(generic_dir)
            if f.lower().endswith(
                (
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp"
                )
            )
        ]

    if not candidates:
        raise RuntimeError(
            "No fallback art available"
        )

    chosen = random.choice(
        candidates
    )

    filename = os.path.splitext(
        os.path.basename(chosen)
    )[0]

    try:
        title, artist = filename.split(
            "__",
            1
        )
    except ValueError:
        title = filename
        artist = "Unknown"

    return {
        "path": chosen,
        "title": title,
        "artist": artist
    }

def get_cover_and_credit(parsed):

    if parsed.get("cover_url"):

        try:

            return {
                "image": load_image(
                    parsed["cover_url"]
                ),
                "credit": parsed.get(
                    "art_credits",
                    "Unknown"
                )
            }

        except Exception:
            pass

    fallback = get_fallback_art(
        parsed["system"]
    )

    return {
        "image": load_image(
            fallback["path"]
        ),
        "credit": (
            f"{fallback['title']} "
            f"— "
            f"{fallback['artist']}"
        )
    }

# --------------------------------------------------
# Font Helpers
# --------------------------------------------------

def get_font(
    font_key,
    size=None
):

    cfg = POSTER_FONTS[font_key]

    return ImageFont.truetype(
        cfg["file"],
        size or cfg["size"]
    )

# --------------------------------------------------
# Text Helpers
# --------------------------------------------------

def wrap_text(
    draw,
    text,
    font,
    max_width
):

    wrapped_lines = []

    paragraphs = text.split("\n")

    for paragraph in paragraphs:

        if not paragraph.strip():
            wrapped_lines.append("")
            continue

        words = paragraph.split()

        current = ""

        for word in words:

            candidate = (
                word
                if not current
                else f"{current} {word}"
            )

            width = draw.textbbox(
                (0, 0),
                candidate,
                font=font
            )[2]

            if width <= max_width:
                current = candidate

            else:
                if current:
                    wrapped_lines.append(current)

                current = word

        if current:
            wrapped_lines.append(current)

    return wrapped_lines


def fit_text_box(
    draw,
    text,
    font_key,
    max_width,
    max_height
):

    cfg = POSTER_FONTS[font_key]

    for size in range(
        cfg["size"],
        cfg["min_size"] - 1,
        -2
    ):

        font = get_font(
            font_key,
            size
        )

        lines = wrap_text(
            draw,
            text,
            font,
            max_width
        )

        line_height = (
            draw.textbbox(
                (0, 0),
                "Ag",
                font=font
            )[3]
            + cfg.get("line_spacing", 8)
        )

        total_height = (
            line_height *
            len(lines)
        )

        if total_height <= max_height:
            return (
                font,
                lines,
                line_height
            )

    font = get_font(
        font_key,
        cfg["min_size"]
    )

    lines = wrap_text(
        draw,
        text,
        font,
        max_width
    )

    line_height = (
        draw.textbbox(
            (0, 0),
            "Ag",
            font=font
        )[3]
        + cfg.get("line_spacing", 8)
    )

    return (
        font,
        lines,
        line_height
    )


def draw_region_text(
    draw,
    text,
    region_name,
    color_key
):

    if not text:
        return

    region = POSTER_LAYOUT["regions"][region_name]

    font_key = region["font"]

    color = POSTER_THEME["colors"][color_key]

    shadow_x=POSTER_THEME["shadow"]["x"]
    shadow_y = POSTER_THEME["shadow"]["y"]
    shadow_color = tuple(POSTER_THEME["shadow"]["color"])

    font, lines, line_height = fit_text_box(
        draw,
        text,
        font_key,
        region["width"],
        region["height"]
    )

    total_height = (
            len(lines) * line_height
    )

    valign = region.get(
        "valign",
        "top"
    )

    if valign == "middle":

        y = (
                region["y"]
                + (
                        region["height"]
                        - total_height
                ) // 2
        )

    elif valign == "bottom":

        y = (
                region["y"]
                + region["height"]
                - total_height
        )

    else:
        y = region["y"]

    align = region.get(
        "align",
        "left"
    )

    for line in lines:

        bbox = draw.textbbox(
            (0, 0),
            line,
            font=font
        )

        text_width = bbox[2] - bbox[0]

        if align == "center":

            x = (
                    region["x"]
                    + (
                            region["width"]
                            - text_width
                    ) // 2
            )

        elif align == "right":

            x = (
                    region["x"]
                    + region["width"]
                    - text_width
            )

        else:
            x = region["x"]

        draw.text(
            (x + shadow_x, y + shadow_y),
            line,
            fill=shadow_color,
            font=font
        )

        draw.text(
            (x, y),
            line,
            fill=color,
            font=font
        )

        y += line_height


# --------------------------------------------------
# Poster Generation
# --------------------------------------------------

def generate_poster(
    parsed,
    teaser="",
    offset_x=None,
    offset_y=None
):

    cover = get_cover_and_credit(parsed)
    img = cover["image"]

    canvas = prepare_background(img, offset_x, offset_y)

    draw = ImageDraw.Draw(canvas)

    draw_region_text(
        draw,
        parsed["title"],
        "title",
        "title"
    )

    type_text = (
        f"An {parsed['game_type']} {parsed['system']} {parsed['session_type']}"
    )

    draw_region_text(
        draw,
        type_text,
        "type",
        "subtitle"
    )

    draw_region_text(
        draw,
        (
            f"{parsed['session_date']}, from {parsed['session_time']}"
        ),
        "datetime",
        "body"
    )

    draw_region_text(
        draw,
        f"DM: {parsed['dm']}",
        "dm",
        "body"
    )

    if teaser:

        draw_region_text(
            draw,
            teaser,
            "teaser",
            "teaser"
        )

    draw_region_text(
        draw,
        (
            f"Art: {cover['credit']}"
        ),
        "credits",
        "credits"
    )

    os.makedirs(
        "output",
        exist_ok=True
    )
    
    safe_title = re.sub(r"[^a-zA-Z0-9]+", "-", parsed["title"]).strip("-").lower()

    path = (
        f"output/{safe_title}-poster.png"
    )

    canvas.convert("RGB").save(
        path,
        quality=95
    )

    return path
    
