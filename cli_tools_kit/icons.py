"""Emoji and system icons for the tool list."""

from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional

from . import state

# Pillow renders tool icons. It is an optional extra (``cli-tools-kit[gui]``);
# without it the GUI still runs, just without per-tool icon thumbnails, and the
# headless paths (--list / --check) work regardless.
try:
    from PIL import Image, ImageTk
    _HAVE_PIL = True
except ImportError:  # pragma: no cover - exercised only on minimal installs
    Image = None
    ImageTk = None
    _HAVE_PIL = False


def get_system_icons() -> List[tuple]:
    """Get list of system icons available on the system.

    Returns:
        List of (icon_name, icon_path) tuples, sorted by name
    """
    icons = {}
    for name, path in iter_system_icons():
        # Prefer larger/higher quality versions
        if name not in icons or 'scalable' in path or '128' in path:
            icons[name] = path
    # Sort by name
    return sorted(icons.items(), key=lambda x: x[0].lower())


def iter_system_icons():
    """Iterate through system icons, yielding (name, path) as they're found.

    This is a generator that yields icons incrementally, useful for background loading.
    """
    icon_dirs = [
        "/usr/share/icons/hicolor/48x48/apps",
        "/usr/share/icons/hicolor/64x64/apps",
        "/usr/share/icons/hicolor/128x128/apps",
        "/usr/share/icons/hicolor/scalable/apps",
        "/usr/share/icons/Adwaita/48x48/apps",
        "/usr/share/icons/Adwaita/64x64/apps",
        "/usr/share/icons/breeze/apps/48",
        "/usr/share/icons/breeze/apps/64",
        "/usr/share/pixmaps",
        os.path.expanduser("~/.local/share/icons/hicolor/48x48/apps"),
        os.path.expanduser("~/.local/share/icons/hicolor/128x128/apps"),
    ]

    for icon_dir in icon_dirs:
        if not os.path.isdir(icon_dir):
            continue
        try:
            for f in os.listdir(icon_dir):
                if f.endswith(('.png', '.svg', '.xpm')):
                    name = os.path.splitext(f)[0]
                    yield (name, os.path.join(icon_dir, f))
        except (OSError, PermissionError):
            continue


# Emoji collection for icon browser - generated from Unicode emoji ranges
def _build_emoji_list():
    """Build emoji list dynamically from Unicode emoji codepoint ranges."""
    import unicodedata
    _EMOJI_RANGES = [
        (0x2600, 0x26FF),    # Misc symbols (sun, cloud, umbrella, etc)
        (0x2700, 0x27BF),    # Dingbats (scissors, pencil, etc)
        (0x1F300, 0x1F5FF),  # Misc Symbols and Pictographs
        (0x1F600, 0x1F64F),  # Emoticons (faces)
        (0x1F680, 0x1F6FF),  # Transport and Map
        (0x1F7E0, 0x1F7EB),  # Large colored circles/squares
        (0x1F900, 0x1F9FF),  # Supplemental Symbols (animals, people, objects)
        (0x1FA70, 0x1FAFF),  # Symbols Extended-A (newer emoji)
    ]
    result = []
    for start, end in _EMOJI_RANGES:
        for cp in range(start, end + 1):
            try:
                name = unicodedata.name(chr(cp))
                result.append((chr(cp), name.lower()))
            except ValueError:
                continue
    return result


EMOJI_LIST = _build_emoji_list()


def _get_emoji_cache_dir():
    """Get/create emoji icon cache directory."""
    cache_dir = os.path.join(state.IDENTITY.cache_path, "emoji_icons")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _find_emoji_font():
    """Find a color emoji font on the system."""
    font_paths = [
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto-color-emoji/NotoColorEmoji.ttf",
        "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
        "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/google-noto-color-emoji/NotoColorEmoji.ttf",
        "/usr/share/fonts/truetype/unifont/unifont.ttf",
    ]
    # Windows ships Segoe UI Emoji; macOS ships Apple Color Emoji. Neither has
    # fc-match, so without these the lookup below finds nothing and every emoji
    # falls back to the monochrome glyph.
    windir = os.environ.get("WINDIR", r"C:\Windows")
    font_paths += [
        os.path.join(windir, "Fonts", "seguiemj.ttf"),
        "/System/Library/Fonts/Apple Color Emoji.ttc",
    ]
    for path in font_paths:
        if os.path.exists(path):
            return path
    # Try fc-match as last resort
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", ":family=Noto Color Emoji"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip() and os.path.exists(result.stdout.strip()):
            return result.stdout.strip()
    except Exception:
        pass
    return None


def render_emoji_icon(emoji_char: str, output_path: str, size: int = 128) -> bool:
    """Render an emoji character to a PNG file.

    Uses NotoColorEmoji at its native bitmap size (109) and scales to target.
    Falls back to monochrome rendering if color font unavailable.
    Returns True if successful.
    """
    from PIL import ImageDraw, ImageFont

    # Method 1: Color emoji font at native bitmap size, then scale
    font_path = _find_emoji_font()
    if font_path:
        # NotoColorEmoji is a CBDT bitmap font with fixed sizes.
        # Try the native size (109 for Noto), then common alternatives.
        for native_size in [109, 128, 64, 48, 32, 16]:
            try:
                font = ImageFont.truetype(font_path, native_size)
                break
            except OSError:
                continue
        else:
            font = None

        if font is not None:
            try:
                # Draw at a fixed offset on a canvas with room on every side,
                # then let the crop below find the glyph. Centering by textbbox
                # first would cut emoji off: for a character with a variation
                # selector (U+2600 U+FE0F) Segoe UI Emoji reports a box half
                # again as wide as what it actually paints, and the resulting
                # offset pushes the right-hand side off the canvas.
                render_size = native_size * 3
                img = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)

                origin = native_size // 2
                draw.text((origin, origin), emoji_char, font=font, embedded_color=True)

                if img.getbbox():
                    # Crop to content, resize to target with padding
                    cropped = img.crop(img.getbbox())
                    # Scale to fit within size with margin
                    target_inner = int(size * 0.85)
                    cropped.thumbnail((target_inner, target_inner), Image.Resampling.LANCZOS)
                    final = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                    ox = (size - cropped.width) // 2
                    oy = (size - cropped.height) // 2
                    final.paste(cropped, (ox, oy))
                    final.save(output_path)
                    return True
            except (TypeError, Exception):
                pass

    # Method 2: Fallback monochrome rendering with DejaVu or default font
    try:
        font_size = int(size * 0.7)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        bbox = draw.textbbox((0, 0), emoji_char, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x = (size - w) // 2 - bbox[0]
        y = (size - h) // 2 - bbox[1]

        draw.text((x, y), emoji_char, font=font, fill=(255, 255, 255, 255))

        if img.getbbox():
            img.save(output_path)
            return True
    except Exception:
        pass

    return False


# ================= ICON UTILITIES =================

def _get_current_icon_theme() -> str:
    """Get current icon theme from gsettings or kdeglobals."""
    # Try gsettings (GNOME)
    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "icon-theme"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            return result.stdout.strip().strip("'")
    except Exception:
        pass

    # Try KDE config
    kde_config = os.path.expanduser("~/.config/kdeglobals")
    if os.path.exists(kde_config):
        try:
            with open(kde_config) as f:
                for line in f:
                    if line.startswith("Theme="):
                        return line.split("=", 1)[1].strip()
        except Exception:
            pass

    return "hicolor"  # Fallback


def find_icon_path(icon_name: str, size: int = 32) -> Optional[str]:
    """Find the path to an icon file from the system theme.

    Args:
        icon_name: The freedesktop icon name (e.g., "applications-graphics")
        size: Preferred icon size in pixels

    Returns:
        Path to the icon file, or None if not found
    """
    if not icon_name:
        return None

    # If it's already a path, return it
    if os.path.isabs(icon_name) and os.path.exists(icon_name):
        return icon_name

    # Icon theme directories (prioritize user's current theme)
    theme = _get_current_icon_theme()
    icon_dirs = [
        # User's current theme first (matches desktop appearance)
        f"/usr/share/icons/{theme}",
        os.path.expanduser(f"~/.local/share/icons/{theme}"),
        # Breeze variants (KDE default)
        "/usr/share/icons/breeze-dark",
        "/usr/share/icons/breeze",
        # Fallback themes
        "/usr/share/icons/hicolor",
        os.path.expanduser("~/.local/share/icons/hicolor"),
        "/usr/share/icons/Adwaita",
        "/usr/share/icons/gnome",
        "/usr/share/pixmaps",
    ]

    # Preferred sizes (in order of preference)
    sizes = [str(size), "32", "48", "24", "64", "22", "16", "256"]

    # Extensions to try (SVG supported via ImageMagick convert)
    extensions = [".svg", ".png", ".xpm"]

    for icon_dir in icon_dirs:
        if not os.path.exists(icon_dir):
            continue

        # Try size-specific directories
        for sz in sizes:
            for category in ["apps", "categories", "mimetypes", "actions", "places", "devices"]:
                for ext in extensions:
                    # Standard freedesktop structure: theme/size/category/name.ext
                    path = os.path.join(icon_dir, f"{sz}x{sz}", category, f"{icon_name}{ext}")
                    if os.path.exists(path):
                        return path
                    # Some themes use: theme/category/size/name.ext
                    path = os.path.join(icon_dir, category, sz, f"{icon_name}{ext}")
                    if os.path.exists(path):
                        return path

        # Try scalable SVGs last (they're harder to load)
        for category in ["apps", "categories", "mimetypes", "actions", "places", "devices"]:
            path = os.path.join(icon_dir, "scalable", category, f"{icon_name}.svg")
            if os.path.exists(path):
                return path

        # Try direct lookup in pixmaps
        for ext in extensions:
            path = os.path.join(icon_dir, f"{icon_name}{ext}")
            if os.path.exists(path):
                return path

    return None


def load_icon_image(icon_name: str, size: int = 24) -> Optional[ImageTk.PhotoImage]:
    """Load an icon as a PhotoImage for use in tkinter.

    Args:
        icon_name: The freedesktop icon name
        size: Desired size in pixels

    Returns:
        PhotoImage object, or None if icon couldn't be loaded
    """
    path = find_icon_path(icon_name, size)
    if not path:
        return None

    try:
        if path.endswith(".svg"):
            # Convert SVG to PNG using rsvg-convert (best quality) or fallbacks
            import io

            # Try rsvg-convert first (proper SVG rendering with correct colors)
            result = subprocess.run(
                ["rsvg-convert", "-w", str(size), "-h", str(size), path],
                capture_output=True, timeout=5
            )
            if result.returncode == 0 and result.stdout:
                img = Image.open(io.BytesIO(result.stdout))
            else:
                # Fallback: try cairosvg
                try:
                    import cairosvg
                    png_data = cairosvg.svg2png(url=path, output_width=size, output_height=size)
                    img = Image.open(io.BytesIO(png_data))
                except (ImportError, Exception):
                    # Last resort: ImageMagick (may have color issues)
                    result = subprocess.run(
                        ["convert", "-background", "none", "-resize", f"{size}x{size}", path, "png:-"],
                        capture_output=True, timeout=5
                    )
                    if result.returncode != 0:
                        return None
                    img = Image.open(io.BytesIO(result.stdout))
        else:
            img = Image.open(path)
            img = img.resize((size, size), Image.Resampling.LANCZOS)

        # Convert to RGBA if necessary
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        return ImageTk.PhotoImage(img)
    except Exception:
        return None


# --- Thread-safe PIL image cache for async icon browser ---
_pil_image_cache: Dict[tuple, Optional[Image.Image]] = {}


def load_pil_image(path: str, size: int) -> Optional[Image.Image]:
    """Load an icon file as a PIL Image (thread-safe, cached).

    Unlike load_icon_image(), this takes a resolved file path (no icon name lookup)
    and returns a PIL Image instead of PhotoImage (which must be created on the main thread).
    """
    key = (path, size)
    if key in _pil_image_cache:
        return _pil_image_cache[key]

    try:
        if path.endswith(".svg"):
            import io
            result = subprocess.run(
                ["rsvg-convert", "-w", str(size), "-h", str(size), path],
                capture_output=True, timeout=5
            )
            if result.returncode == 0 and result.stdout:
                img = Image.open(io.BytesIO(result.stdout))
            else:
                try:
                    import cairosvg
                    png_data = cairosvg.svg2png(url=path, output_width=size, output_height=size)
                    img = Image.open(io.BytesIO(png_data))
                except (ImportError, Exception):
                    result = subprocess.run(
                        ["convert", "-background", "none", "-resize", f"{size}x{size}", path, "png:-"],
                        capture_output=True, timeout=5
                    )
                    if result.returncode != 0:
                        _pil_image_cache[key] = None
                        return None
                    img = Image.open(io.BytesIO(result.stdout))
        else:
            img = Image.open(path)
            img = img.resize((size, size), Image.Resampling.LANCZOS)

        if img.mode != "RGBA":
            img = img.convert("RGBA")

        img.load()  # Detach from file handle
        _pil_image_cache[key] = img
        return img
    except Exception:
        _pil_image_cache[key] = None
        return None
