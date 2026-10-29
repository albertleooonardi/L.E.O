# Benchmark 2026-10-29 22:42 (before smart routing)

Cheap: `nvidia/nemotron-3.5-lightning:free` · Strong: `gemini-2.5-flash` · L.E.O. roles: generator=nvidia/nemotron-3.5-lightning:free, planner=gemini-2.5-flash, reviewer=gemini-2.5-flash · max output 8192

Rebuilt from the run log after the report step crashed (fixed since).

| Approach | Passed | Mean tokens | Tokens per passed task | Mean calls | Mean seconds |
|---|---|---|---|---|---|
| single-cheap | 5/6 | 4,448 | 5,337 | 1.0 | 53 |
| single-strong | 5/6 | 1,172 | 1,406 | 1.3 | 22 |
| leo-quick | 4/6 | 3,210 | 4,814 | 1.2 | 58 |
| leo-reviewed | 4/6 | 6,088 | 9,132 | 2.3 | 97 |
| leo-deep | 3/3 | 18,813 | 18,813 | 6.3 | 180 |

| Task | cheap | strong | quick | reviewed | deep |
|---|---|---|---|---|---|
| roman | pass 6,139 | pass 1,609 | pass 5,734 | pass 2,307 | pass 15,465 |
| intervals | pass 2,451 | pass 1,164 | pass 2,511 | pass 2,856 | — |
| duration | FAIL 7,726 | pass 1,315 | FAIL 1,628 | FAIL 814 | pass 25,649 |
| topk | pass 4,401 | pass 1,013 | FAIL 4,371 | FAIL 7,687 | pass 15,326 |
| lru | pass 2,858 | pass 1,931 | pass 2,202 | pass 13,224 | — |
| ipv4 | pass 3,111 | FAIL 0 | pass 2,811 | pass 9,641 | — |
