"""The ai-branch-guard CI job: what AI branches may and may not change."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "gitlab-duo" / "project-template" / ".gitlab" / "ci" / "ai_branch_guard.py"
spec = importlib.util.spec_from_file_location("ai_branch_guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = guard
spec.loader.exec_module(guard)
Change = guard.Change


def _check(branch, changes, pusher="alice", before=None, after=None):
    return guard.check(branch, pusher, changes, (before or {}).get, (after or {}).get)


def test_people_on_normal_branches_are_not_checked():
    assert _check("feature/x", [Change("M", "src/app.py"), Change("D", "old.py")]) == []


@pytest.mark.parametrize("pusher", ["ai-devassist-tests-acme", "duo-bot", "GitLabDuo"])
def test_ai_accounts_may_only_push_to_ai_branches(pusher):
    problems = _check("develop", [], pusher=pusher)
    assert problems and "may only push to ai/*" in problems[0]
    assert _check("ai/tests-231", [Change("A", "tests/test_x.py")], pusher=pusher) == []


def test_tests_branch_only_touches_tests():
    ok = [Change("A", "tests/test_order_service.py"), Change("A", "pkg/tests/helpers.py"), Change("M", "src/foo_test.go"), Change("A", "web/cart.spec.ts")]
    assert _check("ai/tests-231", ok) == []
    problems = _check("ai/tests-231", [Change("M", "src/payments/discount.py")])
    assert problems == ["src/payments/discount.py: an ai/tests-* branch may only add or change test files."]


@pytest.mark.parametrize("path", [".gitlab-ci.yml", ".gitlab/duo/agent-config.yml", "AGENTS.md", "CODEOWNERS", "skills/x/SKILL.md", "tests/.gitlab-ci.yml"])
def test_protected_paths(path):
    assert "protected path" in _check("ai/tests-1", [Change("M", path)])[0]


def test_no_deletions_on_ai_branches():
    assert "may not delete" in _check("ai/fix-5512", [Change("D", "src/a.py")])[0]


def test_docs_branch_accepts_docs_and_docstring_only_changes():
    before = {"src/svc.py": "def pay(amount):\n    return amount * 2  # double\n"}
    after = {"src/svc.py": 'def pay(amount):\n    """Charge twice the amount."""\n    # payment is doubled on purpose\n    return amount * 2\n'}
    changes = [Change("A", "docs/api.md"), Change("M", "README.md"), Change("M", "CHANGELOG.md"), Change("M", "src/svc.py")]
    assert _check("ai/docs-api", changes, before=before, after=after) == []


def test_docs_branch_rejects_code_changes_hidden_in_a_docstring_edit():
    before = {"src/svc.py": "def pay(amount):\n    return amount * 2\n"}
    after = {"src/svc.py": 'def pay(amount):\n    """Charge the amount."""\n    return amount * 3\n'}
    problems = _check("ai/docs-api", [Change("M", "src/svc.py")], before=before, after=after)
    assert problems == ["src/svc.py: an ai/docs-* branch changed code, not only docstrings and comments."]


def test_docs_branch_rejects_other_files():
    assert "only change documentation" in _check("ai/docs-x", [Change("M", "src/app.ts")])[0]
    assert "only change documentation" in _check("ai/docs-x", [Change("A", "src/new.py")])[0]
    assert "only change documentation" in _check("ai/docs-x", [Change("M", "requirements.txt")])[0]


def test_same_code_ignores_formatting_but_not_logic():
    assert guard.same_code("x = 1\n", "x  =  1  # one\n")
    assert not guard.same_code("x = 1\n", "x = 2\n")
    assert not guard.same_code("x = 1\n", "x = (\n")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_main_against_a_real_repository(tmp_path, monkeypatch):
    """The job as CI runs it: real git history, GitLab's variables."""

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "dev@example.com")
    git("config", "user.name", "Dev")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "svc.py").write_text("def pay(a):\n    return a\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()
    git("checkout", "-qb", "ai/tests-1")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_svc.py").write_text("def test_pay():\n    assert True\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "tests")
    monkeypatch.chdir(tmp_path)
    env = {"CI_COMMIT_REF_NAME": "ai/tests-1", "GITLAB_USER_LOGIN": "ai-devassist-tests-acme", "CI_MERGE_REQUEST_DIFF_BASE_SHA": base}
    assert guard.main(env) == 0

    (tmp_path / "src" / "svc.py").write_text("def pay(a):\n    return a + 1\n", encoding="utf-8")
    git("commit", "-qam", "sneaky fix")
    assert guard.main(env) == 1
    assert guard.main({**env, "CI_COMMIT_REF_NAME": "develop"}) == 1
