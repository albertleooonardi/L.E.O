"""Colour themes for the terminal. The UI only names styles (leo.accent, leo.warn, …); a theme says what they look like.

The choice is saved in config.json (`theme`) and can be changed with /theme.
"""
from prompt_toolkit.styles import Style
from rich.theme import Theme

STYLES = ("accent", "muted", "ok", "warn", "error", "brand")

THEMES = {
    "dark": {
        "label": "Dark", "description": "soft blues and violet, for a dark terminal",
        "banner": ["#7dd3fc", "#67c6f9", "#5ab8f5", "#60a5fa", "#818cf8", "#a78bfa"],
        "accent": "#67c6f9", "muted": "grey58", "ok": "#4ade80", "warn": "#facc15", "error": "#f87171", "brand": "#60a5fa",
        "code": "monokai", "select": "#67c6f9 bold",
    },
    "light": {
        "label": "Light", "description": "deeper blues and violet, for a light terminal",
        "banner": ["#0369a1", "#0b5fa5", "#1d4ed8", "#3730a3", "#4c1d95", "#6d28d9"],
        "accent": "#0e7490", "muted": "grey42", "ok": "#15803d", "warn": "#a16207", "error": "#b91c1c", "brand": "#1d4ed8",
        "code": "friendly", "select": "#1d4ed8 bold",
    },
    "contrast": {
        "label": "High contrast", "description": "bright, bold colours that are easy to tell apart",
        "banner": ["bright_white"] * 3 + ["bright_cyan"] * 3,
        "accent": "bold bright_cyan", "muted": "white", "ok": "bold bright_green", "warn": "bold bright_yellow",
        "error": "bold bright_red", "brand": "bold bright_white", "code": "native", "select": "bold ansibrightcyan",
    },
    "terminal": {
        "label": "Terminal colours", "description": "only the 16 colours of your terminal's own palette",
        "banner": ["cyan"] * 3 + ["blue"] * 3,
        "accent": "cyan", "muted": "dim", "ok": "green", "warn": "yellow", "error": "red", "brand": "blue",
        "code": "ansi_dark", "select": "ansicyan bold",
    },
    "mono": {
        "label": "No colour", "description": "plain text with bold and dim only",
        "banner": ["bold"] * 6,
        "accent": "bold", "muted": "dim", "ok": "bold", "warn": "bold", "error": "bold reverse", "brand": "default",
        "code": "bw", "select": "bold reverse",
    },
}
DEFAULT = "dark"
_active = {"name": DEFAULT}


def name(value=None):
    """A known theme name: `value` if it is one, else the default."""
    return value if value in THEMES else DEFAULT


def use(value):
    """Make `value` the active theme (for code highlighting); returns the rich Theme to give a Console."""
    _active["name"] = name(value)
    return rich_theme(_active["name"])


def apply(console):
    """Give a console the active theme's styles; returns it."""
    console.push_theme(rich_theme(_active["name"]))
    return console


def current():
    return _active["name"]


def rich_theme(value):
    spec = THEMES[name(value)]
    styles = {f"leo.{key}": spec[key] for key in STYLES}
    styles.update({f"leo.banner{i}": f"bold {color}" if color != "bold" else "bold" for i, color in enumerate(spec["banner"])})
    styles["leo.lead"] = f"bold {spec['ok']}" if not spec["ok"].startswith("bold") else spec["ok"]
    return Theme(styles)


def code():
    """The syntax-highlighting theme for code, diffs, and Markdown code blocks."""
    return THEMES[_active["name"]]["code"]


def prompt_style(value=None):
    """prompt_toolkit's colours for menus: the selected line in the theme's accent."""
    spec = THEMES[name(value or _active["name"])]
    return Style.from_dict({"selected-option": spec["select"], "number": "", "frame.border": "", "input-selection": spec["select"]})
