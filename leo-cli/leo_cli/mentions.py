"""@path in a message: attach a file's text or a folder's listing, as typed in other terminal agents.

`fix the bug in @src/app.py` sends the message with the file's current text after it, so the agent starts with it
instead of spending a step on reading it. A folder (`@src/`) attaches its listing. Paths are relative to the session's
folder; `~/...` and absolute paths work too, and a path outside the folder is attached as read-only context (the
agent's own tools still stay inside the folder). An @ that is not a path, such as an email address or `@everyone`,
is left as typed.
"""
import os
import re
from pathlib import Path

from .workspace import SKIP

# An @ at the start of a word: @path, or @"a path with spaces".
MENTION = re.compile(r'(?<!\S)@(?:"([^"]+)"|(\S+))')
FILE_CHARS = 100_000  # Per file; the rest is left for the agent to read with its own tools.
TOTAL_CHARS = 300_000
LIST_ENTRIES = 300


def resolve(token, root):
    """The existing path a mention names, or None; trailing punctuation (`see @a.py,`) is not part of the path."""
    for candidate in (token, token.rstrip(".,;:!?)]}'\"")):
        if not candidate:
            continue
        path = Path(os.path.expanduser(candidate))
        path = path if path.is_absolute() else Path(root) / path
        if path.exists():
            return path.resolve()
    return None


def shown(path, root):
    """How the message names an attached path: relative inside the folder, absolute outside it."""
    root = os.path.realpath(root)
    text = str(path)
    return os.path.relpath(text, root) if text == root or text.startswith(root + os.sep) else text


def listing(folder):
    lines = []

    def walk(directory, level):
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for child in children:
            if len(lines) >= LIST_ENTRIES or child.name in SKIP:
                continue
            directory_child = child.is_dir() and not child.is_symlink()
            lines.append(os.path.relpath(child, folder) + ("/" if directory_child else ""))
            if directory_child and level < 2:
                walk(child, level + 1)

    walk(folder, 1)
    return lines


def attach(text, root):
    """The message with every mentioned file and folder attached, and one note per attachment for the screen."""
    seen, blocks, notes, budget = set(), [], [], TOTAL_CHARS
    for match in MENTION.finditer(text):
        path = resolve(match.group(1) or match.group(2), root)
        if path is None or path in seen:
            continue
        seen.add(path)
        name = shown(path, root)
        if path.is_dir():
            entries = listing(path)
            more = "\n… (listing cut)" if len(entries) >= LIST_ENTRIES else ""
            blocks.append(f'<folder path="{name}">\n' + "\n".join(entries) + more + "\n</folder>")
            notes.append(f"{name.rstrip('/')}/ · {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            notes.append(f"{name} · not attached ({exc.strerror or exc})")
            continue
        if b"\0" in data[:8000]:
            notes.append(f"{name} · binary file, not attached")
            continue
        body = data.decode("utf-8", "replace")
        limit = min(FILE_CHARS, budget)
        if limit <= 0:
            notes.append(f"{name} · not attached (the message is already long; the agent can read it)")
            continue
        cut = len(body) > limit
        body = body[:limit] + (f"\n… (cut here; {len(data):,} bytes in all, read the rest with read_file)" if cut else "")
        budget -= len(body)
        blocks.append(f'<file path="{name}">\n{body}\n</file>')
        count = body.count("\n") + (0 if body.endswith("\n") else 1)
        notes.append(f"{name} · {count} line{'' if count == 1 else 's'}{' (cut)' if cut else ''}")
    if not blocks:
        return text, notes
    return text + "\n\nAttached by the user with @ (as the files are now):\n\n" + "\n\n".join(blocks), notes


def complete(word, root, limit=50):
    """Completions for an @word being typed: (replacement text, label) for the files and folders it could name."""
    typed = word[1:].strip('"')
    folder, prefix = os.path.split(typed)
    base = Path(os.path.expanduser(folder)) if folder else Path(".")
    base = base if base.is_absolute() else Path(root) / base
    try:
        children = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return []
    found = []
    for child in children:
        if child.name in SKIP or not child.name.startswith(prefix) or (child.name.startswith(".") and not prefix.startswith(".")):
            continue
        label = child.name + ("/" if child.is_dir() else "")
        path = os.path.join(folder, label)
        found.append(('@"' + path + '"' if " " in path else "@" + path, label))
        if len(found) >= limit:
            break
    return found
