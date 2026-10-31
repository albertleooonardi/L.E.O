"""The routing head: match a task to a skill and difficulty, then pick the cheapest capable model.

Classification here is rule-based and costs no tokens. For Deep runs the planner model labels each subtask
with a skill and difficulty instead; those labels are validated against the same skill list.
"""
import re

TIERS = {"light": 0, "standard": 1, "strong": 2}
# Thinking effort sent to reasoning models by task difficulty; planning and review count as light.
EFFORT = {"light": "low", "standard": "low", "strong": "medium"}
# Tokens assumed for one call before a model has history (input, output).
TYPICAL_CALL = (3000, 1500)


def call_tokens(c):
    """Average tokens per call from this model's completed calls, or a typical call when it has none."""
    return (c.get("avg_input_tokens") or TYPICAL_CALL[0], c.get("avg_output_tokens") or TYPICAL_CALL[1])


def call_cost(c):
    """Estimated dollars for one call: free and local models cost nothing; paid ones by price and observed token use."""
    if c.get("free") or c.get("local"):
        return 0.0
    tokens_in, tokens_out = call_tokens(c)
    return ((c.get("input_price") or 0) * tokens_in + (c.get("output_price") or 0) * tokens_out) / 1_000_000

# Starter skills seeded on first start; owners can edit, disable, delete, or add their own.
STARTER_SKILLS = [
    {"id": "general", "name": "General", "description": "Conversation, explanations, writing, and summaries.",
     "keywords": ["explain", "summarize", "summary", "write", "email", "letter", "translate", "what is", "why", "how does", "describe", "compare", "list", "idea", "story"],
     "instructions": "Answer directly and concisely. Use short paragraphs or lists when they help."},
    {"id": "frontend", "name": "Frontend", "description": "Websites, landing pages, HTML, CSS, JavaScript, and UI components.",
     "keywords": ["html", "css", "javascript", "website", "web page", "webpage", "landing page", "frontend", "front-end", "ui", "react", "vue", "svelte", "tailwind", "responsive", "navbar", "component", "page", "site", "dom", "animation"],
     "instructions": "For a web page, return one complete, self-contained file in a single ```html block with CSS in <style> and JavaScript in <script>, so it can be previewed directly. Use semantic HTML, a responsive layout, and accessible contrast; never put light text on a busy image. Design like a careful designer: a restrained palette of 3 to 5 colors as CSS variables, one or two typefaces with a clear type scale, generous whitespace on a consistent spacing scale. Give it real depth: for an informative page, 5 to 7 sections and 400+ words of specific content for the subject (facts, numbers, names, how-tos), never bracketed placeholders or lorem ipsum. Make every link and button point to something on the page, and give every section a link target id. Avoid external dependencies except web fonts unless asked."},
    {"id": "backend", "name": "Backend", "description": "APIs, servers, databases, authentication, and integrations.",
     "keywords": ["api", "backend", "back-end", "server", "endpoint", "database", "sql", "postgres", "mysql", "sqlite", "fastapi", "flask", "django", "express", "node", "auth", "authentication", "rest", "graphql", "webhook", "orm", "migration", "checkout", "payment"],
     "instructions": "Write production-minded backend code: validate input, handle errors explicitly, never hard-code secrets, and state how to run it. Keep the data model and endpoints consistent."},
    {"id": "coding", "name": "Coding", "description": "Functions, algorithms, debugging, refactoring, and tests.",
     "keywords": ["function", "python", "algorithm", "bug", "debug", "refactor", "class", "test", "unit test", "typescript", "java", "rust", "go", "regex", "parse", "implement", "code", "script", "error", "exception", "complexity"],
     "instructions": "Return complete, runnable code in fenced blocks. Handle every rule and edge case the request names, validate input exactly as specified (for example, str.isdigit() also accepts non-ASCII digits), and keep explanations brief. Give one final version only: no drafts and no 'let me revise'."},
    {"id": "design", "name": "Design", "description": "Visual design, UX, branding, color, typography, and layout.",
     "keywords": ["design", "ux", "user experience", "color", "colour", "palette", "typography", "font", "branding", "brand", "logo", "layout", "wireframe", "style guide", "mockup", "aesthetic", "theme", "minimalist", "elegant", "sleek", "beautiful", "clean design", "clean look", "modern look"],
     "instructions": "Make specific, justified design choices: name colors as hex values, typefaces, spacing, and hierarchy. Prefer a distinctive direction that fits the subject over generic defaults."},
    {"id": "research", "name": "Research", "description": "Finding current information on the web and summarizing it with sources.",
     "keywords": ["search", "research", "sources", "references", "cite", "citations", "latest", "news", "look up", "find out", "reviews", "prices", "compare prices"],
     "instructions": "Use the provided sources for facts that may have changed and cite them as [n]. Say when sources disagree or do not cover something. End with a short Sources list of the URLs you used."},
    {"id": "reasoning", "name": "Reasoning", "description": "Math, logic, proofs, planning under constraints, and analysis.",
     "keywords": ["prove", "proof", "calculate", "math", "equation", "probability", "logic", "puzzle", "optimize", "optimise", "derive", "statistics", "estimate", "analyze", "analyse", "trade-off", "tradeoff"],
     "instructions": "Reason step by step, check the result, and state the final answer clearly."},
]

BUILD_NOUNS = r"app|application|website|web page|webpage|landing page|site|page|api|server|backend|system|platform|project|game|dashboard|store|shop|bot|cli|script"
BUILD_WORDS = re.compile(rf"\b(build|create|make|develop|design|generate)\b.*?\b({BUILD_NOUNS})\b", re.I | re.S)
# Requests that need fresh or external information; HEAD plans a web search for these (when search is set up).
SEARCH_CUES = re.compile(r"\b(search (for|the web|online|the internet)|web search|look ?up|google it|browse the web|on the (web|internet)|"
                         r"latest|newest|most recent|breaking|news|right now|as of (today|now)|today's|this (week|month)'s|"
                         r"current (prices?|version|events?|weather|rates?|status)|prices? of|how much (is|are|does|do)|"
                         r"sources|with references|cite|citations?|according to|reviews of|weather)\b", re.I)
URLS = re.compile(r"https?://[^\s<>()\"']+[^\s<>().,;:!?\"']")
# Short questions asking for an explanation or a quick transformation are light work, whatever their topic.
SIMPLE_QUESTION = re.compile(r"^\s*(what|who|when|where|why|how (do|does|is|are|can)|explain|define|what's|is|are|can|translate|summari[sz]e)\b", re.I)
# One self-contained page is a single deliverable: one capable model writes it better than a plan-then-build chain.
PAGE_NOUNS = {"website", "web page", "webpage", "landing page", "site", "page"}
APP_NOUNS = {"app", "application", "api", "server", "backend", "system", "platform", "game", "dashboard", "bot", "cli", "script"}
REQUIREMENT_WORDS = re.compile(r"\b(only|exactly|must|raise|raises|error|invalid|valid|at most|at least|no leading|not allowed|"
                               r"edge cases?|reject|must not|without|allowed)\b", re.I)
HARD_WORDS = re.compile(r"\b(complete|full|production|entire|end-to-end|architecture|scalable|multi-page|multiple pages|with (a )?backend|secure|optimi[sz]e|prove)\b", re.I)


def _hits(text, keywords):
    return sum(1 for k in keywords if re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", text))


def classify(text, skills):
    """Skill scores, the best skill, and a difficulty from free text signals."""
    lowered = text.lower()
    enabled = [s for s in skills if s.get("enabled", True)]
    scores = {s["id"]: _hits(lowered, s.get("keywords", [])) for s in enabled}
    order = {s["id"]: i for i, s in enumerate(enabled)}
    # Ties go to a specific skill over General (the fallback), then to the skill listed first.
    ranked = sorted(((v, k) for k, v in scores.items() if v), key=lambda vk: (vk[0], vk[1] != "general", -order[vk[1]]))
    skill = ranked[-1][1] if ranked else ("general" if any(s["id"] == "general" for s in enabled) else (enabled[0]["id"] if enabled else None))
    matched = [k for v, k in ranked if v]
    build = BUILD_WORDS.search(text)
    if build:
        # The thing being built ("…a website") decides the main deliverable, not the most frequent keyword.
        nouns = re.findall(rf"\b({BUILD_NOUNS})\b", text[build.start():].lower())
        owner = next((s["id"] for noun in nouns for s in enabled if noun in s.get("keywords", [])), None)
        skill = owner or skill
    parts = len(re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", text)) + len(re.findall(r"\b(and also|as well as|plus|then)\b", lowered))
    signals = []
    built = set(re.findall(rf"\b({BUILD_NOUNS})\b", text[build.start():].lower())) if build else set()
    single_page = (bool(built & PAGE_NOUNS) and not built & APP_NOUNS and set(matched) <= {"frontend", "design", "general"}
                   and not HARD_WORDS.search(text) and parts < 2 and len(text) <= 1200)
    if single_page:
        signals.append("single-page build")
    elif build:
        signals.append("build request")
    if HARD_WORDS.search(text):
        signals.append("scope words")
    if len(matched) >= 3:
        signals.append(f"{len(matched)} skills involved")
    if parts >= 2:
        signals.append("multiple parts")
    if len(text) > 1200:
        signals.append("long request")
    simple = bool(SIMPLE_QUESTION.match(text)) and len(text) <= 160 and not signals and "```" not in text
    scope = [s for s in signals if s != "single-page build"]  # A page with style words is still one standard deliverable.
    if simple:
        difficulty = "light"
        skill = skill if skill in {"reasoning", "research"} else ("general" if any(s["id"] == "general" for s in enabled) else skill)
    elif "build request" in signals or len(scope) >= 2 or (scope and len(matched) >= 2):
        difficulty = "strong"
    elif signals or len(text) > 280 or matched and matched[-1] in {"coding", "backend", "reasoning", "frontend"}:
        difficulty = "standard"
    else:
        difficulty = "light"
    # Requests that spell out rules and edge cases can be checked; Auto mode has those answers reviewed.
    checkable = len(REQUIREMENT_WORDS.findall(text)) >= 2
    urls = list(dict.fromkeys(URLS.findall(text)))[:3]
    search = bool(urls or SEARCH_CUES.search(text))
    if search:
        signals.append("needs web" if not urls else "reads given URL")
    return {"skill": skill, "difficulty": difficulty, "skills": matched[::-1], "signals": signals, "search": search, "urls": urls,
            "checkable": checkable}


# Skills whose results a reviewer can check for correctness. Design, frontend, and writing reviews mostly
# produce cosmetic nitpicks and a full rewrite, so Auto mode skips review for them.
REVIEWABLE = {"coding", "backend", "reasoning"}


def wants_review(route, mode):
    return mode == "reviewed" or (mode == "deep" and (not route.get("auto") or route["skill"] in REVIEWABLE))


def choose_mode(route):
    """Auto mode: spend extra calls only where they are likely to pay off."""
    if route["difficulty"] == "strong":
        return "deep" if len(route["skills"]) >= 2 or "build request" in route["signals"] else "reviewed"
    if route["difficulty"] == "standard" and route.get("checkable") and route["skill"] in REVIEWABLE:
        return "reviewed"  # Stated rules and edge cases are where a cheap model slips and a reviewer catches it.
    return "quick"


def rank(connections, skill, difficulty, prefs, pinned=None):
    """Order eligible connections for a task, with a reason for the first choice.

    Prefer a model strong enough for the task, then one that has the skill, then (for light work) one without hidden
    reasoning, then the lightest sufficient tier (cheaper). Reasoning models also get a low effort setting on simple work
    (see EFFORT). Ties follow the fallback order setting. With "auto", every enabled model competes: among models that
    fit, the cheapest per call wins (free ones cost nothing; paid ones are priced from their own past token use).
    """
    need = TIERS.get(difficulty, 1)
    route = prefs.get("routing", "auto")
    auto = route == "auto"

    def key(c):
        tier = TIERS.get(c.get("tier", "standard"), 1)
        # "General" is not a specialty: models listing it compete with generalists on tier and cost.
        has = skill != "general" and skill in c.get("skills", [])
        generalist = not c.get("skills") or "general" in c.get("skills", [])
        enough = tier >= need
        return (
            c["id"] != pinned if pinned else False,
            not enough,  # Never hand a task to a model too light for it while a capable one is available.
            not has, not generalist and not has,
            round(call_cost(c), 6) if auto else 0,
            bool(c.get("reasoning")) and need == 0,  # Among fitting models, avoid hidden reasoning tokens on light work.
            tier - need if enough else need - tier,
            bool(c.get("reasoning")) and need < 2,
            not c["local"] if route in ("local_first", "auto") else not c["free"] if route == "free_first" else False,
            c["priority"], c["id"],
        )

    ordered = sorted(connections, key=key)
    if not ordered:
        return [], ""
    first = ordered[0]
    reasons = []
    if pinned and first["id"] == pinned:
        reasons.append("assigned in Settings")
    if skill == "general":
        reasons.append("general work: lightest capable model")
    elif skill in first.get("skills", []):
        reasons.append(f"has the {skill} skill")
    elif any(skill in c.get("skills", []) for c in ordered):
        reasons.append(f"models with the {skill} skill are too light for {difficulty} work")
    else:
        reasons.append("no model lists this skill; best general fit")
    tier = first.get("tier", "standard")
    reasons.append(f"{tier} tier fits {difficulty} work" if TIERS.get(tier, 1) >= need else f"strongest available ({tier}) for {difficulty} work")
    if first.get("reasoning"):
        reasons.append("reasoning model")
    if auto:
        cost = call_cost(first)
        reasons.append("free" if not cost else f"about ${cost:.4f} per call, cheapest fit")
    return ordered, "; ".join(reasons)
