---
applyTo: "src/**/*.py"
---

# Python source

- Target Python 3.10+. Use type hints and `from __future__ import annotations` like the existing modules.
- Keep the DevAssist rule: facts are measured by code, the model only adds judgement. Never let a model output become a count, status or link without checking it.
- `src/devassist/gitlab.py` must keep its write allow-list. Do not add merge, delete, deploy or protected-branch operations.
- `src/devassist/masking.py` runs before any log text reaches a model. Do not bypass it; add a test for every new pattern.
- Docstrings and comments in English. Keep changes small and covered by tests in `tests/`.
