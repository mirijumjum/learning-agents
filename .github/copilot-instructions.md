# Repository instructions for GitHub Copilot

This repository holds two Python packages under `src/`:

- `agent_lab`: a small CLI that teaches Microsoft Agent Framework (coder, tester and runner agents).
- `devassist`: development agents (tests, docs, Git, trace) for Microsoft Teams. Docs: `docs/DEVASSIST.md` (Spanish).

## Build and test

- Python 3.10 or newer. Install with `pip install -e ".[dev]"`.
- Run the whole suite with `python -m pytest -q`. It is offline: no API key, model or network is needed.
- The release gate also runs `devassist eval --profile mock`.
- Tests live in `tests/` and use plain pytest functions, `monkeypatch` and `tmp_path`. Follow the style of the existing files.
- If you need the local GitLab stand-in, start it with `devassist fake-gitlab --port 8936`. It listens only on 127.0.0.1.

## Rules for every change (people and agents)

- Work on a branch and open a pull request. Agent branches are `copilot/*` (Copilot cloud agent) or `ai/*`.
- Never merge, approve, deploy, force-push, or delete branches, tags, releases, issues or files outside the task.
- Never push to `main`. The default branch is protected by a ruleset (see `docs/github-copilot/ruleset-main.json`).
- Do not edit `.github/workflows/`, `.gitlab-ci.yml`, `infra/` or `Dockerfile*` unless the task is about them. Say so in the pull request if you do.
- Do not add secrets, tokens or real personal data to code, tests, fixtures or logs. Use obviously fake values such as `user@example.com`.
- Keep the safety guarantees in `src/devassist/gitlab.py` (write allow-list, no merge/delete) and `src/devassist/masking.py`. Never weaken them to make a test pass.
- Report facts measured by tools (test counts, coverage, pipeline status). Do not invent numbers or links.

## Pull request description

State what changed, how it was tested (the exact command and its result), and anything left unverified.
