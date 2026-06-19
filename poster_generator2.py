import os
import yaml
import random
import logging
import requests
import re

from functools import lru_cache
from io import BytesIO
from PIL import (
    Image,
    ImageDraw,
    ImageFont,
    ImageFilter,
    UnidentifiedImageError
)

Image.MAX_IMAGE_PIXELS = 100_000_000

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_WIDTH = 20000
MAX_IMAGE_HEIGHT = 20000
MAX_IMAGE_PIXELS_ALLOWED = 100_000_000

from pathlib import Path

MAX_TEXT_LENGTH = 10000
MAX_TEMPLATE_OUTPUT_LENGTH = 10000
MAX_WORD_LENGTH = 500

MAX_CANVAS_WIDTH = 10000
MAX_CANVAS_HEIGHT = 10000
MAX_CANVAS_PIXELS = 100_000_000

# --------------------------------------------------
# Logger
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# --------------------------------------------------
# Paths
# --------------------------------------------------

POSTERS_ROOT = Path("posters")

GENERAL_CONFIG = POSTERS_ROOT / "general.yaml"
FORMATS_DIR = POSTERS_ROOT / "formats"
FONTS_DIR = POSTERS_ROOT / "fonts"
ART_DIR = POSTERS_ROOT / "art"

ART_DIR.mkdir(
    parents=True,
    exist_ok=True
)

OUTPUT_DIR = POSTERS_ROOT / "output"

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True
)

# --------------------------------------------------
# Config Cache
# --------------------------------------------------

POSTER_GENERAL = {}
POSTER_FORMATS = {}
FORMATS_DICT = {}
LAYOUTS_DICT = {}
ART_DICT = {}

# --------------------------------------------------
# Config Loading
# --------------------------------------------------

def load_yaml(path):

    with open(
        path,
        "r",
        encoding="utf-8"
    ) as f:

        return yaml.safe_load(f)


def reload_poster_configs():

    global POSTER_GENERAL
    global POSTER_FORMATS
    global FORMATS_DICT
    global LAYOUTS_DICT

    POSTER_GENERAL = load_yaml(GENERAL_CONFIG)
    validate_general_config(POSTER_GENERAL)

    POSTER_GENERAL["shadow"]["colour"] = parse_colour(POSTER_GENERAL["shadow"]["colour"])

    POSTER_FORMATS = {}
    FORMATS_DICT = {}
    LAYOUTS_DICT = {}

    for yaml_file in FORMATS_DIR.glob("*.yaml"):
        try:
            cfg = load_yaml(yaml_file)
            POSTER_FORMATS[yaml_file.stem] = cfg
            FORMATS_DICT[yaml_file.stem] = cfg["format"]
            for layout in cfg["layouts"]:
                name = f"{cfg['format']} - {layout}"
                LAYOUTS_DICT[name] = (yaml_file.stem, layout)

        except Exception as e:
            logger.error(
                f"Failed loading "
                f"{yaml_file.name}: {e}"
            )

    logger.info(
        f"Loaded {len(POSTER_FORMATS)} poster formats and {len(LAYOUTS_DICT)} layouts."
    )

    reload_art_index()
    get_font.cache_clear()

def update_poster_layouts():

    return sorted(LAYOUTS_DICT.keys())

# --------------------------------------------------
# Layout Helpers
# --------------------------------------------------

def get_available_formats():

    return sorted(
        POSTER_FORMATS.keys()
    )


def get_available_layouts(
    format_name
):

    format_cfg = POSTER_FORMATS[
        format_name
    ]

    return sorted(
        format_cfg[
            "layouts"
        ].keys()
    )

# --------------------------------------------------
# Definitions
# --------------------------------------------------

def get_layout(
    format_name,
    layout_name
):

    format_cfg = POSTER_FORMATS[
        format_name
    ]

    return format_cfg[
        "layouts"
    ][layout_name]

# --------------------------------------------------
# Definition Properties
# --------------------------------------------------

def get_element_config(
    format_cfg,
    definition
):

    cfg = (
        format_cfg
        .get("defaults", {})
        .copy()
    )

    cfg.update(definition)

    if "colour" in cfg:
        cfg["colour"] = parse_colour(cfg["colour"])
    else:
        raise ValueError(
            f"Missing colour in definition: {definition}"
        )

    return cfg

# --------------------------------------------------
# Text Rendering
# --------------------------------------------------

def render_text(
    template,
    parsed,
    teaser,
    art_credits
):

    context = parsed.copy()

    context["teaser"] = teaser
    context["art_credits"] = art_credits

    if len(template) > MAX_TEXT_LENGTH:
        raise ValueError(
            "Template exceeds maximum length"
        )

    for key, value in context.items():
        if isinstance(value, str):
            if len(value) > MAX_TEXT_LENGTH:
                raise ValueError(
                    f"{key} exceeds maximum length"
                )

    result = template.format(
        **context
    )

    if len(result) > MAX_TEMPLATE_OUTPUT_LENGTH:
        raise ValueError(
            "Rendered text too long"
        )

    return result

# --------------------------------------------------
# Load and process Image
# --------------------------------------------------

def load_image(source):

    if str(source).startswith("http"):

        response = requests.get(
            source,
            timeout=30,
            stream=True,
            allow_redirects=True
        )

        response.raise_for_status()

        content_length = int(
            response.headers.get(
                "Content-Length",
                0
            )
        )

        if (
            content_length
            and content_length > MAX_IMAGE_BYTES
        ):
            raise ValueError(
                "Image exceeds maximum size"
            )

        data = bytearray()

        for chunk in response.iter_content(
            chunk_size=8192
        ):

            data.extend(chunk)

            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError(
                    "Image exceeds maximum size"
                )

        try:

            img = Image.open(BytesIO(data))
            width, height = img.size
            if width * height > MAX_IMAGE_PIXELS_ALLOWED:
                raise ValueError("Image dimensions too large")
            img.load()

        except UnidentifiedImageError:
            raise ValueError("Invalid image file")

    else:

        size = os.path.getsize(source)

        if size > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds maximum size")

        img = Image.open(source)
        width, height = img.size
        if width * height > MAX_IMAGE_PIXELS_ALLOWED:
            raise ValueError("Image dimensions too large")
        img.load()

    if (
        img.width > MAX_IMAGE_WIDTH
        or img.height > MAX_IMAGE_HEIGHT
    ):
        raise ValueError(
            f"Image dimensions too large: "
            f"{img.width}x{img.height}"
        )

    return img

def scale_to_fill(
    img,
    target_width,
    target_height,
    overscan=1.0
):

    scale = max(
        target_width / img.width,
        target_height / img.height
    )

    scale *= max(
        1.0,
        min(overscan, 2.0)
    )

    return img.resize(
        (
            int(img.width * scale),
            int(img.height * scale)
        ),
        Image.Resampling.LANCZOS
    )

def crop_to_canvas(
    img,
    canvas_cfg,
    offset_x=None,
    offset_y=None
):

    width = canvas_cfg["width"]
    height = canvas_cfg["height"]

    left = (
        img.width - width
    ) // 2

    top = (
        img.height - height
    ) // 2

    if offset_x is None:

        offset_x = canvas_cfg.get(
            "image_offset_x",
            0
        )

    if offset_y is None:

        offset_y = canvas_cfg.get(
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

def prepare_background(
    img,
    canvas_cfg,
    offset_x=None,
    offset_y=None,
    overscan=1.0
):

    try:

        width = canvas_cfg["width"]
        height = canvas_cfg["height"]

        img = img.convert(
            "RGBA"
        )

        img = scale_to_fill(
            img,
            width,
            height,
            overscan
        )

        img = crop_to_canvas(
            img,
            canvas_cfg,
            offset_x,
            offset_y
        )

        bg_cfg = POSTER_GENERAL[
            "background"
        ]

        img = img.filter(
            ImageFilter.GaussianBlur(
                radius=bg_cfg[
                    "blur_radius"
                ]
            )
        )

        opacity = int(
            255
            * bg_cfg[
                "darken_opacity"
            ]
        )

        overlay = Image.new(
            "RGBA",
            img.size,
            (
                0,
                0,
                0,
                opacity
            )
        )

        img.alpha_composite(
            overlay
        )

        return img

    except MemoryError:

        raise RuntimeError(
            "Image processing exceeded memory limits"
        )

# --------------------------------------------------
# Text Formatting
# --------------------------------------------------

def parse_colour(value):

    if isinstance(value, str):

        value = value.strip()

        if not value.startswith("#"):
            raise ValueError(
                f"Invalid hex colour: {value}"
            )

        hex_value = value[1:]

        if len(hex_value) == 6:

            r = int(hex_value[0:2], 16)
            g = int(hex_value[2:4], 16)
            b = int(hex_value[4:6], 16)

            return (r, g, b)

        elif len(hex_value) == 8:

            r = int(hex_value[0:2], 16)
            g = int(hex_value[2:4], 16)
            b = int(hex_value[4:6], 16)
            a = int(hex_value[6:8], 16)

            return (r, g, b, a)

        raise ValueError(f"Invalid hex colour: {value}")

    if isinstance(value, list):

        if len(value) not in (3, 4):
            raise ValueError(
                "Colour must contain 3 or 4 values"
            )

        for channel in value:

            if (
                not isinstance(channel, int)
                or channel < 0
                or channel > 255
            ):
                raise ValueError(
                    f"Invalid colour channel: {channel}"
                )

        return tuple(value)

    raise ValueError(
        f"Unsupported colour value: {value}"
    )

@lru_cache(maxsize=64)
def get_font(
    font_path,
    size
):

    return ImageFont.truetype(
        POSTERS_ROOT / font_path,
        size
    )

def wrap_text(
    draw,
    text,
    font,
    max_width
):
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(
            "Text exceeds maximum length"
        )

    wrapped_lines = []

    paragraphs = text.split("\n")

    for paragraph in paragraphs:

        if not paragraph.strip():
            wrapped_lines.append("")
            continue

        words = paragraph.split()

        current = ""

        for word in words:

            if len(word) > MAX_WORD_LENGTH:
                raise ValueError("Word exceeds maximum length")

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
                    wrapped_lines.append(
                        current
                    )

                current = word

        if current:
            wrapped_lines.append(
                current
            )

    return wrapped_lines

def fit_text_box(
    draw,
    text,
    cfg
):

    for size in range(
        cfg["size"],
        cfg["min_size"] - 1,
        -2
    ):

        font = get_font(
            cfg["font"],
            size
        )

        lines = wrap_text(
            draw,
            text,
            font,
            cfg["width"]
        )

        line_height = (
            draw.textbbox(
                (0, 0),
                "Ag",
                font=font
            )[3]
            + cfg["line_spacing"]
        )

        total_height = (
            len(lines)
            * line_height
        )

        widest = max(
        	draw.textbbox(
        		(0, 0),
        		line,
        		font=font
        	)[2]
        	-
        	draw.textbbox(
        		(0, 0),
        		line,
        		font=font
        	)[0]
        	for line in lines
        )
        
        if (total_height <= cfg["height"] and widest <= cfg["width"]):

            return (
                font,
                lines,
                line_height
            )

    font = get_font(
        cfg["font"],
        cfg["min_size"]
    )

    lines = wrap_text(
        draw,
        text,
        font,
        cfg["width"]
    )

    line_height = (
        draw.textbbox(
            (0, 0),
            "Ag",
            font=font
        )[3]
        + cfg["line_spacing"]
    )

    return (
        font,
        lines,
        line_height
    )

# --------------------------------------------------
# Element Rendering
# --------------------------------------------------

def draw_element(
    draw,
    cfg,
    text
):

    if not text:
        return

    font, lines, line_height = fit_text_box(
        draw,
        text,
        cfg
    )

    total_height = (
        len(lines)
        * line_height
    )

    valign = cfg.get(
        "valign",
        "top"
    )

    if valign == "middle":

        y = (
            cfg["y"]
            + (
                cfg["height"]
                - total_height
            ) // 2
        )

    elif valign == "bottom":

        y = (
            cfg["y"]
            + cfg["height"]
            - total_height
        )

    else:

        y = cfg["y"]

    align = cfg.get(
        "align",
        "left"
    )

    shadow_cfg = POSTER_GENERAL[
        "shadow"
    ]

    shadow_enabled = shadow_cfg.get(
        "enabled",
        True
    )

    shadow_x = shadow_cfg.get(
        "x",
        0
    )

    shadow_y = shadow_cfg.get(
        "y",
        0
    )

    shadow_colour = shadow_cfg["colour"]

    for line in lines:

        bbox = draw.textbbox(
            (0, 0),
            line,
            font=font
        )

        text_width = (bbox[2] - bbox[0])

        if align == "center":

            x = (
                cfg["x"]
                + (cfg["width"] - text_width) // 2
                - bbox[0]
            )

        elif align == "right":

            x = (
                cfg["x"]
                + cfg["width"]
                - text_width
                - bbox[0]
            )

        else:

            x = cfg["x"] - bbox[0]

        if shadow_enabled:

            draw.text(
                (
                    x + shadow_x,
                    y + shadow_y
                ),
                line,
                fill=shadow_colour,
                font=font
            )

        draw.text(
            (x, y),
            line,
            fill=cfg["colour"],
            font=font
        )

        y += line_height

def render_definition(
    draw,
    format_cfg,
    definition,
    parsed,
    teaser,
    art_credits
):

    cfg = get_element_config(
        format_cfg,
        definition
    )

    text = render_text(
        cfg["value"],
        parsed,
        teaser,
        art_credits
    )

    draw_element(
        draw,
        cfg,
        text
    )

# --------------------------------------------------
# Art and Credits
# --------------------------------------------------

def reload_art_index():

    global ART_DICT
    ART_DICT = {}

    for system_dir in ART_DIR.iterdir():
        if not system_dir.is_dir():
            continue
        system_name = system_dir.name

        for art_file in system_dir.iterdir():
            if art_file.suffix.lower() not in (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp"
            ):
                continue
            filename = art_file.stem

            try:
                title, artist = filename.split(
                    "__",
                    1
                )

            except ValueError:
                title = filename
                artist = "Unknown"

            display_name = (
                f"{system_name} - "
                f"{title} - "
                f"{artist}"
            )

            ART_DICT[display_name] = {
                "path": art_file,
                "filename": art_file.name,
                "title": title,
                "artist": artist,
                "system": system_name
            }

    logger.info(
        f"Loaded {len(ART_DICT)} artwork entries."
    )

def update_poster_art():
    return sorted(ART_DICT.keys())

def get_art_override(art_name):
    if art_name not in ART_DICT:
        raise ValueError(
            f"Unknown artwork: {art_name}"
        )
    return ART_DICT[art_name]

def get_fallback_art(system):

    system_dir = ART_DIR / system

    generic_dir = ART_DIR / "generic"

    candidates = []

    if system_dir.exists():

        candidates = [

            system_dir / f

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

            generic_dir / f

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
            "No fallback art available."
        )

    chosen = random.choice(
        candidates
    )

    filename = chosen.stem

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

def get_cover_and_credit(parsed, art_override=None):

    if art_override:
        art = get_art_override(art_override)
        return {
            "image": load_image(art["path"]),
            "credit": f"{art['artist']}"
        }

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

            logger.warning(
                "Cover URL failed, "
                "falling back to local art."
            )

    fallback = get_fallback_art(
        parsed["system"]
    )

    return {

        "image": load_image(
            fallback["path"]
        ),

        "credit": (
            f"{fallback['artist']}"
        )
    }

# --------------------------------------------------
# Poster Generation
# --------------------------------------------------

def generate_poster(
    parsed,
    #format_name="square-1080x1080",
    #layout_name=None,
    preset="Square - Standard",
    teaser="",
    offset_x=None,
    offset_y=None,
    overscan=1.0,
    art_override=None
):
    try:
        # ------------------------------------------
        # Extract layout and format
        # ------------------------------------------

        if preset not in LAYOUTS_DICT:
            raise ValueError(
                f"Unknown preset: {preset}"
            )
        format_name, layout_name = LAYOUTS_DICT[preset]
        format_clean = FORMATS_DICT[format_name]

        # ------------------------------------------
        # Validate format
        # ------------------------------------------

        if format_name not in POSTER_FORMATS:

            raise ValueError(
                f"Unknown format: {format_name}"
            )

        format_cfg = POSTER_FORMATS[
            format_name
        ]

        # ------------------------------------------
        # Auto-select layout
        # ------------------------------------------

        if layout_name is None:

            layout_name = next(
                iter(
                    format_cfg["layouts"]
                )
            )

        # ------------------------------------------
        # Validate layout
        # ------------------------------------------

        if (
            layout_name
            not in format_cfg["layouts"]
        ):

            raise ValueError(
                f"Unknown layout "
                f"'{layout_name}' "
                f"for format "
                f"'{format_name}'"
            )

        layout = format_cfg[
            "layouts"
        ][layout_name]

        # ------------------------------------------
        # Resolve artwork
        # ------------------------------------------

        cover = get_cover_and_credit(parsed, art_override)

        img = cover["image"]

        art_credits = cover["credit"]

        # ------------------------------------------
        # Build background
        # ------------------------------------------

        canvas = prepare_background(
            img,
            format_cfg["canvas"],
            offset_x,
            offset_y,
            overscan
        )

        draw = ImageDraw.Draw(
            canvas
        )

        # ------------------------------------------
        # Render elements
        # ------------------------------------------

        for element_name in layout[
            "elements"
        ]:

            if (
                element_name
                not in layout[
                    "definitions"
                ]
            ):

                logger.warning(
                    f"Element "
                    f"'{element_name}' "
                    f"missing definition."
                )

                continue

            definition = layout[
                "definitions"
            ][element_name]

            render_definition(
                draw,
                format_cfg,
                definition,
                parsed,
                teaser,
                art_credits
            )

        # ------------------------------------------
        # Output filename
        # ------------------------------------------

        safe_title = re.sub(
            r"[^a-zA-Z0-9]+",
            "-",
            parsed["title"]
        )

        safe_title = (
            safe_title
            .strip("-")
            .lower()
        )

        safe_title = safe_title[:60]

        filename = (
            f"{safe_title}"
            f" - {format_clean}"
            f" - {layout_name}.png"
        )

        output_path = (
            OUTPUT_DIR
            / filename
        )

        # ------------------------------------------
        # Save
        # ------------------------------------------

        canvas.convert(
            "RGB"
        ).save(
            output_path,
            quality=95
        )

        logger.info(
            f"Poster saved: "
            f"{output_path}"
        )

        return str(
            output_path
        )

    except MemoryError:
        logger.exception("Poster generation ran out of memory")
        raise RuntimeError("Poster generation failed due to memory constraints")


# --------------------------------------------------
# Font Management
# --------------------------------------------------

def save_font(filename, data):

    ext = Path(filename).suffix.lower()

    if ext not in (".ttf", ".otf"):
        raise ValueError("Only .ttf and .otf fonts are supported")

    path = FONTS_DIR / filename

    with open(path, "wb") as f:
        f.write(data)

    try:
        ImageFont.truetype(path, 20)
    except Exception:
        path.unlink(missing_ok=True)
        raise ValueError(
            "Invalid font file"
        )

    reload_poster_configs()

    return path

def get_font_file(filename):
    path = FONTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(filename)
    return path

def list_fonts():
    fonts = []
    for file in FONTS_DIR.iterdir():
        if not file.is_file():
            continue

        if file.suffix.lower() not in (
            ".ttf",
            ".otf"
        ):
            continue
        fonts.append({
            "name": file.stem,
            "filename": file.name,
            "path": file
        })
    return sorted(
        fonts,
        key=lambda x: x["filename"].lower()
    )

def get_font_choices():
    return [
        font["filename"]
        for font in list_fonts()
    ]

def delete_font(filename):
    path = get_font_file(filename)
    path.unlink()
    reload_poster_configs()

# --------------------------------------------------
# Artwork Management
# --------------------------------------------------

def save_art(system, filename, data):
    system_dir = ART_DIR / system
    system_dir.mkdir(parents=True, exist_ok=True)
    path = system_dir / filename
    with open(path, "wb") as f:
        f.write(data)

    try:
        with Image.open(path) as img:
            img.verify()

        with Image.open(path) as img:
            img.load()

    except Exception:
        path.unlink(missing_ok=True)
        raise ValueError("Invalid image")

    reload_art_index()
    return path


def get_art_file(art_name):
    if art_name not in ART_DICT:
        raise FileNotFoundError(art_name)
    return ART_DICT[art_name]["path"]


def list_art():
    return sorted(ART_DICT.keys())


def delete_art(art_name):
    path = get_art_file(art_name)
    path.unlink()
    reload_art_index()


def get_art_systems():
    systems = []
    for item in ART_DIR.iterdir():
        if item.is_dir():
            systems.append(item.name)
    return sorted(systems)

# --------------------------------------------------
# General Config Validation
# --------------------------------------------------

def validate_general_config(cfg):
    try:
        cfg["background"]["blur_radius"]
        cfg["background"]["darken_opacity"]
        cfg["shadow"]["enabled"]
        cfg["shadow"]["x"]
        cfg["shadow"]["y"]
        parse_colour(cfg["shadow"]["colour"])

    except Exception as e:
        raise ValueError(
            f"Invalid general config: {e}"
        )

    return True

# --------------------------------------------------
# General Config Validation
# --------------------------------------------------

def get_general_config_file():
    return GENERAL_CONFIG

def save_general_config(data):
    if isinstance(data, bytes):
        cfg = yaml.safe_load(data.decode("utf-8"))
    else:
        cfg = yaml.safe_load(data)

    validate_general_config(cfg)

    with open(GENERAL_CONFIG, "wb") as f:
        if isinstance(data, bytes):
            f.write(data)
        else:
            f.write(data.encode("utf-8"))

    reload_poster_configs()
    return GENERAL_CONFIG

# --------------------------------------------------
# Format Config Validation
# --------------------------------------------------

def validate_format_config(cfg, field_map):

    required = [
        "format",
        "canvas",
        "layouts"
    ]

    for key in required:
        if key not in cfg:
            raise ValueError(
                f"Missing key: {key}"
            )

    width = cfg["canvas"]["width"]
    height = cfg["canvas"]["height"]

    if width <= 0 or height <= 0:
        raise ValueError("Invalid canvas size")

    if width > MAX_CANVAS_WIDTH:
        raise ValueError("Canvas width too large")

    if height > MAX_CANVAS_HEIGHT:
        raise ValueError("Canvas height too large")

    if width * height > MAX_CANVAS_PIXELS:
        raise ValueError("Canvas pixel count too large")

    # ------------------------------------------
    # Build dummy context from field_map
    # ------------------------------------------

    dummy_parsed = {
        field_name: ""
        for field_name in field_map.keys()
    }

    # Poster-specific fields that don't come
    # from Notion but are injected later.
    dummy_parsed.update({
        "poster_datetime": "",
        "teaser": "",
        "session_date": "",
        "session_time": "",
        "open_seats": "",
    })

    # ------------------------------------------
    # Dummy renderer
    # ------------------------------------------

    test_image = Image.new(
        "RGBA",
        (
            cfg["canvas"]["width"],
            cfg["canvas"]["height"]
        )
    )

    test_draw = ImageDraw.Draw(
        test_image
    )

    # ------------------------------------------
    # Template field validator
    # ------------------------------------------

    valid_fields = set(dummy_parsed.keys())

    for layout_name, layout in cfg["layouts"].items():

        if "elements" not in layout:
            raise ValueError(
                f"{layout_name}: missing elements"
            )

        if "definitions" not in layout:
            raise ValueError(
                f"{layout_name}: missing definitions"
            )

        for element in layout["elements"]:

            if element not in layout["definitions"]:
                raise ValueError(
                    f"{layout_name}: "
                    f"element '{element}' "
                    f"has no definition"
                )

            definition = layout["definitions"][
                element
            ]

            try:

                merged = get_element_config(
                    cfg,
                    definition
                )

                draw_required_fields = (
                    "font",
                    "value",
                    "size",
                    "min_size",
                    "width",
                    "height",
                    "x",
                    "y",
                    "line_spacing"
                )

                missing = [
                    field
                    for field in draw_required_fields
                    if field not in merged
                ]

                if missing:
                    raise ValueError(
                        f"missing fields: "
                        f"{', '.join(missing)}"
                    )

                # ------------------------------
                # Validate font
                # ------------------------------

                try:

                    get_font(
                        merged["font"],
                        merged["size"]
                    )

                except Exception as e:

                    raise ValueError(
                        f"invalid font: {e}"
                    )

                # ------------------------------
                # Validate placeholders
                # ------------------------------

                placeholders = set(
                    re.findall(
                        r"\{([a-zA-Z0-9_]+)\}",
                        merged["value"]
                    )
                )

                unknown_fields = (
                    placeholders
                    - valid_fields
                )

                if unknown_fields:

                    raise ValueError(
                        "unknown template field(s): "
                        + ", ".join(
                            sorted(
                                unknown_fields
                            )
                        )
                    )

                # ------------------------------
                # Validate render
                # ------------------------------

                render_text(
                    merged["value"],
                    dummy_parsed,
                    "",
                    ""
                )

                render_definition(
                    test_draw,
                    cfg,
                    definition,
                    dummy_parsed,
                    "",
                    ""
                )

            except Exception as e:

                raise ValueError(
                    f"{layout_name}/"
                    f"{element}: "
                    f"{e}"
                )

    return True

# --------------------------------------------------
# Format Config Management
# --------------------------------------------------

def save_format(filename,data, field_map):

    if isinstance(data, bytes):
        cfg = yaml.safe_load(data.decode("utf-8"))
    else:
        cfg = yaml.safe_load(data)

    validate_format_config(cfg, field_map)

    path = FORMATS_DIR / filename

    with open(path,"wb") as f:

        if isinstance(data, bytes):
            f.write(data)

        else:
            f.write(data.encode("utf-8"))

    reload_poster_configs()
    return path


def get_format_file(filename):

    path = FORMATS_DIR / filename

    if not path.exists():
        raise FileNotFoundError(filename)

    return path


def list_formats():

    return sorted(
        [
            file.name
            for file in FORMATS_DIR.glob(
                "*.yaml"
            )
        ]
    )

def delete_format(filename):
    path = get_format_file(filename)
    path.unlink()
    reload_poster_configs()

# --------------------------------------------------
# Testing
# --------------------------------------------------

#reload_poster_configs()

#parsed = {'activate': True, 'art_credits': 'Catto', 'campaign_link': None, 'classes_allowed': '2024 Core + Expanded', 'content_warnings': 'Player vs Player, Harm to Elders, Torture, Mental Illness, Paralysis, Mind Control, Body horror, Gore, Children (Harm/Violence)', 'cost': 'Transport Costs Shared', 'cost_number': None, 'cover_url': 'https://prod-fillout-oregon-s3.s3.us-west-2.amazonaws.com/orgid-362342/flowpublicid-tDpzGmb62sus/a457750a-1749-47eb-a8f1-b63f1926de84-WYGEx52QJLzPxqJsmVbuvn9yp0MJBQR8SF7p9AE1tbOXfdcENe27WECtuiRHoJSR5fjzthtpEPezwiekFnz5rL09Sq4bm14yGMa/WhatsApp-Image-2026-06-15-at-4.05.26-PM.jpg', 'description': '_Every so often, the guards stumble onto a case with odd details, suggesting the presence of a monster or the involvement of magic. They call those the “weird ones”. To deal with the issue, people more versed in these fields are often hired: monster hunters, mages, adventurers…_\n_Four bodies were found in the river with no clearly identifiable cause of death: a textbook “weird one”. The guards know who to call...Ghostbu...No wait, wrong universe._', 'dm': 'Catto', 'end_date': {'end': None, 'start': '2026-06-20T12:30:00.000+00:00', 'time_zone': None}, 'exp_level': 'Newbies', 'expectations': '', 'experience': 'Newbies welcome', 'game_type': 'In-Person', 'level': 4, 'location': 'Bustling Brew Cafe, Thane', 'open_seats': 3, 'other_notes': '- Sess 0, Friday 19 9pm\n- Point buy standard array\n- Couldnt run last week due to health issues :(', 'poster_datetime': 'Saturday, June 20, at 11 AM', 'price_type': 'Paid (Transport Fee only)', 'session_date': 'Saturday, June 20, 2026', 'session_time': '11 AM to 6 PM', 'session_type': 'One-Shot', 'show': True, 'species_allowed': '2024 Core + Expanded', 'start_date': {'end': None, 'start': '2026-06-20T05:30:00.000+00:00', 'time_zone': None}, 'system': 'D&D 2024 (5.5e)', 'title': 'Bloodied Embroidery', 'tsl': 'Casual investigation game'}

#print(update_poster_layouts())
#print(LAYOUTS_DICT)

# generate_poster(
#     parsed = parsed,
#     preset = "Instagram - Standard",
#     teaser = "This is a teaser",
#     offset_x = None,
#     offset_y = None,
#     overscan = 1.0,
#     art_override = None
# )

#cfg = load_yaml("posters/formats/1080p-landscape-1920x1080.yaml")
#field_map = load_yaml("config/field_map.yaml")
#if validate_format_config(cfg, field_map): print("valid")
#print(get_font_choices())
#print("done")
