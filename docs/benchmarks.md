# Historical benchmarks

[← Back to README](../README.md) · [Latest algorithm QA](qa/algorithm-verification-2026-11-11.md)

These are small historical samples from October–November 2026. They preserve the original observations;
provider availability, model names, and routing behavior may have changed. They do not establish a general cost,
speed, or quality advantage over another product.

## Against the Gemini and ChatGPT web apps

The same prompts went to **gemini.google.com** (Gemini 3.5 Flash), **chatgpt.com** (free, logged out), and L.E.O. Auto
with its free models. [`benchmarks/qa.py`](../benchmarks/qa.py) graded every reply the same way: hidden tests for code,
the known answer for the puzzle, and for the page a 14-point checklist, visitor-facing defects, and content measured in
the browser.

| Task | Gemini web | ChatGPT web | L.E.O. (free models) | L.E.O. calls · tokens · time |
| --- | :---: | :---: | :---: | --- |
| IPv4 validator (hidden tests) | ✅ | ✅ | ✅ | 2 · 8.3K · 107 s |
| Duration parser (hidden tests) | ✅ | ✅ | ✅ | 3 · 17.7K · 161 s |
| Roman numerals (hidden tests) | ✅ | ✅ | ✅ | 2 · 7.5K · 54 s |
| Stairs puzzle (answer 927) | ✅ | ✅ | ✅ | 1 · 1.7K · 38 s |
| Coffee landing page: checklist | 79% | 79% | **79–93%** (4 runs) | 1 · 4.0–9.2K · 10–38 s |
| … broken or empty links | 4 | 1 | **0** | |
| … photos | none | 5 hot-linked | **3 verified, credited** | |
| … external scripts | Tailwind CDN | none | **none** | |

In round 1, L.E.O. passed only 1 of 4. A cheap coding model used `str.isdigit()` (which accepts non-ASCII digits) and
left a broken draft before a "let me revise" version, and the page was thin with a broken link. Each fix in the
[harness](architecture.md#the-harness-algorithm) came from that QA loop: the independent reviewer where rules are stated, static checks
that trigger a repair, verified photos, self-correcting reasoning profiles, and continuation of cut-off answers.
ChatGPT's page is the most polished visually; L.E.O.'s has real, credited photos, the most content, and no broken links.
Claude.ai needs a sign-in, so Claude is not in this table yet.

<details>
<summary><b>Earlier benchmarks, and how to run them</b></summary>

[`benchmarks/run.py`](../benchmarks/run.py) sends the same prompts to single models directly ("one app, one model") and
to L.E.O. It checks coding answers against hidden acceptance tests (run in a macOS sandbox with network and writes
blocked), scores websites with a 14-point rubric, and checks the long app task for its required features.
[`benchmarks/report.py`](../benchmarks/report.py) merges result files and recomputes L.E.O.'s **actual** tokens from each
run's call ledger.

```bash
LEO_DATA_DIR=.leo-bench uv run python -m benchmarks.run --single gemini-3.5-flash --single another-model-id --leo auto
uv run python -m benchmarks.report benchmarks/results/result-file.json
```

**30 Oct 2026: L.E.O. Auto with a team of free models.** Team: Gemini 2.5 Flash, Laguna S (coding), Nemotron 3.5
Lightning (reasoning), and Nemotron 3 Ultra 550B (strong). Thinking tokens are counted.

| Approach | Correct | Blocked by provider | Tokens per correct task | Mean time |
| --- | --- | --- | --- | --- |
| Gemini 3.5 Flash alone | 2/2 | 7 of 9 (free quota exhausted) | 2,746 | 17 s |
| **L.E.O. Auto** | **8/9** | **0** | 14,822 | 116 s |
| Nemotron 3 Ultra alone (pages and app only) | 2/3 | 0 | 20,143 | 347 s |

- **Everyday coding:** L.E.O. used fewer tokens than Gemini 3.5 Flash alone on every task both answered (−48% and −22%).
- **Resilience:** L.E.O. answered every task, while the single free model hit its quota after two.
- **Builds:** L.E.O. cost 2–4× the tokens of one strong model; two fixes since then target this, and a re-run is pending.
- **Shared limits:** most models, L.E.O.'s included, failed the IPv4 edge case. Routing cannot add knowledge the models lack.

**29 Oct 2026, before smart routing:** a fixed generator, uncounted thinking tokens, and a context bug in Deep mode,
all fixed in the next version. Full results are in [`benchmarks/results/`](../benchmarks/results). Samples are small;
treat differences as indicative.

**Agent benchmark.** [`benchmarks/agent.py`](../benchmarks/agent.py) runs three tasks end to end with hidden tests: build
a todo app, fix a project's failing tests, and add authentication to an API (50+ steps, research and testing
sub-agents, and a worker restart in the middle).

</details>
