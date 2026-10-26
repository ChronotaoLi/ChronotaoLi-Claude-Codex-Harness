# Personal Harness contributor notes

Read `README.md` for usage and `docs/design.md` for constraints and reference sources.
Runtime code is in `harness/`, common agent instructions in `shared/`, and offline behavior tests in `tests/`.

Keep the core Python 3.11+ and dependency-free. Run `python3 -m unittest discover -s tests -v` after runtime changes.
Tests must exercise lifecycle behavior, acceptance gates, isolation, retrieval or recovery rather than matching prompt wording.

Preserve exact model IDs configured by the user. Workers and reviewers must get fresh processes and dispatch IDs; never add implicit resume, model fallback, recursive agents, or automatic merges.
Do not claim a simulated test proves a model call works. `python3 -m harness smoke` invokes real accounts and is a separate integration check.
Keep private run state under `.harness/`; avoid committing transcripts, credentials or generated MCP configuration.
Keep external-project field-test reports and in-progress test logs under ignored `.harness/`, not in versioned project documentation. `docs/validation.md` records completed Harness verification results, not test progress or external task status.
