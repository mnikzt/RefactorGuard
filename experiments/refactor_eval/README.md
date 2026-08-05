# RefactorGuard Evaluation

This folder contains a lightweight, reproducible experiment setup for interview
metrics. It uses runnable Java Maven repositories rather than isolated snippets.

Commands:

```powershell
uv run python scripts/refactor_eval.py init
uv run python scripts/refactor_eval.py prepare
uv run python scripts/refactor_eval.py smoke
uv run python scripts/refactor_eval.py scan-efficiency --limit 6 --mode both
uv run python scripts/refactor_eval.py summarize
```

Metric wording:

- Baseline: direct LLM repository search with read-only tools.
- Ours: JavaParser / Symbol Solver / PMD CPD candidate generation, then LLM semantic triage.
- Token reduction: `(baseline_tokens - ours_tokens) / baseline_tokens`.
- Time reduction: `(baseline_elapsed - ours_elapsed) / baseline_elapsed`.

The second claim, single-agent vs multi-agent refactoring success, should reuse
the same `tasks.json` rows and record CLI artifacts from `scan`, `plan`, and
`apply --max-repair-attempts N`. Keep the success definition fixed: Maven
verification passes, target smell is removed, public API changes are expected,
and diff/path guards do not reject the patch.
