# Algorithm and 9Router verification — 11 November 2026

The live 9Router connection, three-level CLI task tree, and failure cascade worked in this audit. Two additional policy checks failed: reviewer independence and strongest-available fallback selection. The existing test suite passes but does not catch these cases.

| Check | Observed result |
| --- | --- |
| Repository and CLI tests | **422 passed, 9 skipped**, one existing Starlette/httpx deprecation warning. The skips are opt-in Docker sandbox integration tests requiring `LEO_TEST_SANDBOX_URL`. |
| 9Router tool calls | Nemotron Super VL, North Mini Code, Recursive Flint, Codex `cx/gpt-6-sol`, and Grok `gcli/grok-4.7` each returned the requested native tool call with `answer="OK"`. |
| Unavailable upstream | Gemma 4 31B returned an upstream 429 rate limit, surfaced by 9Router as HTTP 503. This was an upstream failure during the audit, rather than evidence that 9Router had stopped. |
| Live TSATM | Root → manager → worker completed across strong → standard → light tiers. All eight structural checks passed. |
| Live cascade | Gemma failed; the orchestrator tried Nemotron next and completed with exactly `QA_OK`. |
| Additional policy checks | Three passed, two failed, as detailed below. |

## Live TSATM evidence

The isolated CLI session implemented `clamp(value, lower, upper)` and tests, using these roles:

| Role | Model | Observed actions |
| --- | --- | --- |
| Root, strong | Nemotron 3.5 Super VL | Delegated to a manager and accepted its work through `review_work`. |
| Manager, standard | Recursive Flint | Delegated to one worker, inspected evidence, and accepted the worker through `review_work`. |
| Worker, light | North Mini Code | Wrote `clamp.py` and `test_clamp.py`; ran six passing unit tests. |

Both parent roles remained read-only: neither wrote files nor ran shell commands. All child nodes were accepted, and parent links respected tree levels. Independent QA tests then passed: two test methods covering eight normal/boundary cases and invalid bounds.

The worker recovered from `python: command not found` by finding and using `python3`. The run correctly paused at the isolated 14-call budget. One bounded resume completed the root's final response on call 15, with **zero repeated worker calls**. Recorded usage was 88,626 input tokens plus 3,763 output tokens, or **92,389 total**, and $0 estimated model cost. This small task still required 15 calls, so successful operation does not imply minimal overhead.

The QA run used an isolated local database and workspace. Existing model settings were preserved; copied credential key files were removed afterward. Provider quota was consumed.

## Reproduced policy failures

1. **A pinned reviewer can grade its own answer.** With `last_generator="writer"` and the same model assigned as reviewer, `Orchestrator.candidates("reviewer")` returns that writer first even when other models are available. The assigned-model key precedes the author penalty in [orchestrator.py](../../backend/orchestrator.py#L135), and author exclusion is not enforced. This contradicts the stated reviewer-independence policy. A fallback can also reach the author because it remains eligible. Recommended correction: exclude the answer's author from reviewers and explicitly leave the answer unverified when no independent reviewer is available.

2. **Fallback can choose a weaker model and describe it as the strongest available.** For strong coding work with only a free light coding specialist and a free standard generalist, `rank()` picks the light specialist. Skill match precedes tier shortfall in [router.py](../../backend/router.py#L165). Its reason is `has the coding skill; strongest available (light) for strong work; free`, even though a standard model exists. Recommended correction: when no model satisfies the difficulty, prioritize the smallest tier shortfall and generate an accurate explanation.

The other checks confirmed input-order-independent ranking, rejection of a cyclic/forward-dependency plan, and preservation of a valid backward-dependency plan. The policy failures are intentionally preserved as failing audit cases; this audit does not change production selection policy.

## What the algorithms currently implement

- **Model judging:** [discovery.py](../../backend/discovery.py#L47) infers tiers and skills from model metadata, names, families, and related heuristics. It does not measure intelligence using task benchmarks. Selection then uses saved tier, skill, cost, capability, quota, and preference data.
- **Configuration consistency:** The local snapshot contained 29 enabled models: 1 light, 25 standard, and 3 strong. Fourteen Codex entries had saved `standard` tiers while the current discovery heuristic classified them as `strong`. Twenty Codex entries had unknown native-tool capability metadata. The successful Codex probe verifies one model's tool calling, not all twenty. A newer heuristic is also not proof of actual ability; existing labels were not overwritten.
- **Deterministic routing:** [router.py](../../backend/router.py) uses rule-based task classification and deterministic lexicographic ranking. The input-order test passed. Candidate filtering and fallback ordering are deterministic for a given snapshot; availability, usage, and quotas change that snapshot.
- **Dependency plans:** [parse_plan](../../backend/context.py#L96) accepts one to three ordered tasks with dependencies only on earlier tasks. This ensures an acyclic plan; it is a bounded DAG workflow rather than a general DAG scheduler or a trained decision-tree classifier. Existing orchestrator tests exercise dependency results, failure, and resumption.
- **TSATM:** [tree.py](../../backend/tree.py) implements the hierarchical CLI workspace agent. LLMs choose decomposition and review verdicts; structural guards enforce roles, evidence, budgets, retries, and parent relationships. Overall agent behavior is therefore not deterministic. The server/web agent remains flat, as covered by `test_the_server_agent_stays_flat` in [test_tree.py](../../leo-cli/tests/test_tree.py).

## Reproduction and artifacts

The machine-readable observations are in [algorithm-verification-2026-11-11.json](algorithm-verification-2026-11-11.json). The reusable harness is [benchmarks/algorithm_qa.py](../../benchmarks/algorithm_qa.py).

```sh
.venv/bin/python -m pytest tests leo-cli/tests -q
.venv/bin/python -m benchmarks.algorithm_qa --output /tmp/leo-policy-qa
```

The audit command currently exits **1** because the two policy invariants fail. For network verification using the existing account's representative free models:

```sh
.venv/bin/python -m benchmarks.algorithm_qa \
  --live --live-cascade --extend-budget-once \
  --output /tmp/leo-live-algorithm-qa
```

Live mode consumes quota and runs model-generated code in an isolated local workspace through the CLI agent's normal controls. It permits one QA-only tree budget extension, up to 28 calls total. It requires the configured representative model IDs in the harness. Codex/Grok probes and the configuration inventory in the saved observations were additional audit checks; they are not part of the default live harness. Cascade fallback depends on an actual upstream failure, so a future successful Gemma call will not reproduce the same fallback path.

This is evidence that the tested paths work, not a model-quality benchmark or proof for arbitrary workloads. Live retry/escalation under repeated rejected work and large parallel trees were not newly exercised here; their coverage comes from the repository tests. The nine optional sandbox tests remain unverified by this run.
