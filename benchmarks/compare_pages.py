"""Write a side-by-side viewer for the website answers in a benchmark result file.

    uv run python -m benchmarks.compare_pages benchmarks/results/benchmark-....json [task id, default website]

Each page runs in its own sandboxed frame (scripts allowed, no same-origin access, network blocked by CSP),
the same isolation the L.E.O. canvas uses.
"""
import html
import json
import sys
from pathlib import Path

from .run import extract_page

CSP = "default-src 'none'; img-src * data:; style-src 'unsafe-inline' *; font-src * data:; script-src 'unsafe-inline'; connect-src 'none'"


def main(path, task="website"):
    data = json.loads(Path(path).read_text())
    pages = [r for r in data["results"] if r["task"] == task]
    cards = []
    for r in pages:
        page = extract_page(r.get("output") or "")
        tokens = (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
        body = (f'<iframe sandbox="allow-scripts" srcdoc="{html.escape(f"<meta http-equiv=Content-Security-Policy content=\"{CSP}\">" + page)}"></iframe>'
                if page else "<p class=none>No HTML page in this answer.</p>")
        cards.append(f'<section><h2>{html.escape(r["arm"])}</h2><p>{tokens:,} tokens · {r["calls"]} calls · {r["seconds"]}s · '
                     f'{html.escape(r["detail"])}</p>{body}</section>')
    out = Path(path).with_name(f"{Path(path).stem}-{task}.html")
    out.write_text(f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>Page comparison</title>
<style>body{{margin:0;font:14px system-ui;background:#151515;color:#eee}}main{{display:grid;grid-template-columns:repeat({max(len(cards), 1)},1fr);gap:12px;padding:12px}}
section{{display:flex;flex-direction:column;min-width:0}}h2{{margin:0 0 4px;font-size:14px}}p{{margin:0 0 8px;color:#aaa;font-size:12px}}
iframe{{width:100%;height:85vh;border:1px solid #333;border-radius:8px;background:#fff}}.none{{padding:20px;border:1px dashed #555}}</style>
<main>{''.join(cards)}</main></html>""")
    print(out)


if __name__ == "__main__":
    main(*sys.argv[1:3])
