"""Agent skills (SKILL.md folders) and command files, which also become /commands in a session.

A skill is a folder with a SKILL.md whose front matter has a name and a description, plus any scripts or references.
Skills live in ~/.config/leo-cli/skills/ (yours everywhere) and .leo-cli/skills/ (this project; it wins on a name
clash). Command files are prompt templates: .leo-cli/commands/<name>.md, with $ARGUMENTS where the text after the
command goes. These are separate from L.E.O. Chat's routing skills (General, Frontend, Coding...).
"""
import asyncio
import re
import shutil
import tempfile
from pathlib import Path

from backend.sandbox import SandboxError

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_FILES = 60


def front_matter(text):
    """The simple `key: value` lines between the opening --- lines of a SKILL.md."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    found = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep:
            found[key.strip().lower()] = value.strip().strip("'\"")
    return found


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63]


class Skills:
    def __init__(self, project, home):
        self.project, self.home = Path(project), Path(home)

    @property
    def folders(self):
        return [self.home / "skills", self.project / ".leo-cli" / "skills"]  # Later folders win.

    def discover(self):
        found = {}
        for folder in self.folders:
            for manifest in sorted(folder.glob("*/SKILL.md")) if folder.is_dir() else []:
                meta = front_matter(manifest.read_text(errors="replace"))
                name = slug(meta.get("name") or manifest.parent.name)
                if NAME.match(name):
                    found[name] = {"name": name, "description": meta.get("description", "")[:300], "path": manifest.parent}
        return found

    def commands(self):
        found = {}
        for folder in (self.home / "commands", self.project / ".leo-cli" / "commands"):
            for file in sorted(folder.glob("*.md")) if folder.is_dir() else []:
                if NAME.match(file.stem):
                    found[file.stem] = file
        return found

    def catalog(self):
        return "\n".join(f"- {s['name']}: {s['description'] or 'no description'}" for s in self.discover().values())

    def use(self, name):
        skill = self.discover().get(slug(name))
        if not skill:
            raise SandboxError(f"There is no skill called {name}. Skills: {', '.join(self.discover()) or 'none installed'}")
        files = sorted(str(p.relative_to(skill["path"])) for p in skill["path"].rglob("*") if p.is_file() and p.name != "SKILL.md")
        listing = "\n".join(f"- {f}" for f in files[:MAX_FILES]) + (f"\n- … {len(files) - MAX_FILES} more" if len(files) > MAX_FILES else "")
        return (f"Skill {skill['name']} (folder: {skill['path']}). Its files are data and scripts to use as it says; run scripts with bash.\n\n"
                f"{(skill['path'] / 'SKILL.md').read_text(errors='replace')}\n\nFiles:\n{listing or '(none)'}")

    def expand(self, name, arguments=""):
        """The message a /command sends: a command file's template, or a request to use the skill."""
        command = self.commands().get(name)
        if command:
            template = command.read_text(errors="replace")
            return template.replace("$ARGUMENTS", arguments) if "$ARGUMENTS" in template else (template + ("\n\n" + arguments if arguments else ""))
        if name in self.discover():
            return f"Use the {name} skill." + (f" {arguments}" if arguments else "")
        return None

    async def install(self, source):
        """Copy a skill from a local folder, or clone it from git, into this project's skills."""
        source = source.strip()
        with tempfile.TemporaryDirectory() as scratch:
            if re.match(r"^(https://|git@|ssh://)", source):
                process = await asyncio.create_subprocess_exec("git", "clone", "--depth", "1", "--", source, f"{scratch}/repo",
                                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                output, _ = await asyncio.wait_for(process.communicate(), 300)
                if process.returncode:
                    raise SandboxError(f"git clone failed: {output.decode(errors='replace')[-300:]}")
                origin = Path(scratch) / "repo"
            else:
                origin = Path(source).expanduser()
                if not origin.is_absolute():
                    origin = self.project / origin
            manifests = [origin / "SKILL.md"] if (origin / "SKILL.md").is_file() else sorted(origin.glob("*/SKILL.md"))
            if not manifests:
                raise SandboxError(f"No SKILL.md found in {source}")
            installed = []
            for manifest in manifests:
                meta = front_matter(manifest.read_text(errors="replace"))
                name = slug(meta.get("name") or manifest.parent.name)
                if not NAME.match(name):
                    raise SandboxError(f"Invalid skill name in {manifest}")
                target = self.project / ".leo-cli" / "skills" / name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(manifest.parent, target, ignore=shutil.ignore_patterns(".git"), symlinks=True)
                installed.append(f"{name}: {meta.get('description', '')}".rstrip(": "))
        return "Installed " + "; ".join(installed) + ". Each is now available to you with use_skill, and to the user as a /command."
