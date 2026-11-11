"""Colour themes for the terminal. The UI only names styles (leo.accent, leo.warn, …); a theme says what they look like.

The choice is saved in config.json (`theme`) and can be changed with /theme.
"""
from prompt_toolkit.styles import Style
from rich.theme import Theme

STYLES = ("accent", "muted", "ok", "warn", "error", "brand")

THEMES = {
    "dark": {
        "label": "Dark", "description": "neutral text and a quiet blue accent",
        "banner": ["#7dd3fc", "#67c6f9", "#5ab8f5", "#60a5fa", "#818cf8", "#a78bfa"],
        "accent": "#b5cce0", "muted": "#a6a6a6", "ok": "#85bea1", "warn": "#d8b47c", "error": "#e19191", "brand": "#e7e7e7",
        "code": "monokai", "select": "#b5cce0 bold",
        "pt": {"accent": "#b5cce0", "muted": "#a6a6a6", "ok": "#85bea1", "warn": "#d8b47c", "error": "#e19191"},
    },
    "light": {
        "label": "Light", "description": "deeper blues and violet, for a light terminal",
        "banner": ["#0369a1", "#0b5fa5", "#1d4ed8", "#3730a3", "#4c1d95", "#6d28d9"],
        "accent": "#0e7490", "muted": "grey42", "ok": "#15803d", "warn": "#a16207", "error": "#b91c1c", "brand": "#1d4ed8",
        "code": "friendly", "select": "#1d4ed8 bold",
        "pt": {"accent": "#0e7490", "muted": "#6b6b6b", "ok": "#15803d", "warn": "#a16207", "error": "#b91c1c"},
    },
    "contrast": {
        "label": "High contrast", "description": "bright, bold colours that are easy to tell apart",
        "banner": ["bright_white"] * 3 + ["bright_cyan"] * 3,
        "accent": "bold bright_cyan", "muted": "white", "ok": "bold bright_green", "warn": "bold bright_yellow",
        "error": "bold bright_red", "brand": "bold bright_white", "code": "native", "select": "bold ansibrightcyan",
        "pt": {"accent": "bold ansibrightcyan", "muted": "ansiwhite", "ok": "bold ansibrightgreen", "warn": "bold ansibrightyellow",
               "error": "bold ansibrightred"},
    },
    "terminal": {
        "label": "Terminal colours", "description": "only the 16 colours of your terminal's own palette",
        "banner": ["cyan"] * 3 + ["blue"] * 3,
        "accent": "cyan", "muted": "dim", "ok": "green", "warn": "yellow", "error": "red", "brand": "blue",
        "code": "ansi_dark", "select": "ansicyan bold",
        "pt": {"accent": "ansicyan", "muted": "ansibrightblack", "ok": "ansigreen", "warn": "ansiyellow", "error": "ansired"},
    },
    "mono": {
        "label": "No colour", "description": "plain text with bold and dim only",
        "banner": ["bold"] * 6,
        "accent": "bold", "muted": "dim", "ok": "bold", "warn": "bold", "error": "bold reverse", "brand": "default",
        "code": "bw", "select": "bold reverse",
        "pt": {"accent": "bold", "muted": "", "ok": "bold", "warn": "bold", "error": "bold reverse"},
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
    styles["leo.key"] = "bold reverse" if value == "mono" else f"bold reverse {spec['accent'].replace('bold ', '')}"  # A key chip: [ y ].
    styles["leo.border"] = spec["muted"]
    styles["leo.title"] = f"bold {spec['brand']}" if spec["brand"] != "default" else "bold"
    return Theme(styles)


def code():
    """The syntax-highlighting theme for code, diffs, and Markdown code blocks."""
    return THEMES[_active["name"]]["code"]


def prompt_style(value=None):
    """prompt_toolkit's colours: menus, the prompt, the status bar, and key chips, in the theme's palette."""
    spec = THEMES[name(value or _active["name"])]
    pt = spec["pt"]
    key = "bold reverse" if not pt["accent"] or pt["accent"] == "bold" else f"bold reverse {pt['accent'].replace('bold ', '')}"
    return Style.from_dict({
        "selected-option": spec["select"], "number": "", "frame.border": "", "input-selection": spec["select"],
        "prompt": f"bold {pt['accent']}".strip(), "prompt.steer": f"bold {pt['warn']}".strip(),
        "placeholder": f"italic {pt['muted']}".strip(), "key": key, "muted": pt["muted"],
        "bottom-toolbar": "noreverse", "tb": pt["muted"], "tb.current": "bold", "tb.spin": pt["accent"],
        "tb.ok": pt["ok"], "tb.error": pt["error"], "tb.wait": f"bold reverse {pt['warn'].replace('bold ', '')}".strip(),
        "tb.key": f"bold {pt['accent']}".strip(), "tb.autorun": pt["warn"],
    })


def liquid(value=None):
    """Colours for the moving parts of the status bar: (wave gradient, shimmer base, shimmer peak) as hex colours, or
    None for themes that only use the terminal's own colours (they animate with bold instead)."""
    spec = THEMES[name(value or _active["name"])]
    if not all(c.startswith("#") for c in spec["banner"]):
        return None
    return spec["banner"], spec["pt"]["muted"], spec["pt"]["accent"]


def mix(a, b, amount):
    """The hex colour `amount` (0..1) of the way from a to b."""
    x, y = (int(a[i:i + 2], 16) for i in (1, 3, 5)), (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(p + (q - p) * amount):02x}" for p, q in zip(x, y))
