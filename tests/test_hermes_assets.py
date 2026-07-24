from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_wrapper_uses_project_venv_and_emits_collect_stdout():
    text = (ROOT / "deploy/hermes/github-daily-collect.sh").read_text()
    assert "exec .venv/bin/python -m github_daily_reporter.cli collect" in text
    assert "2>&1" not in text


def test_skill_contains_untrusted_data_and_fixed_order_guards():
    text = (ROOT / "deploy/hermes/skills/github-daily-editor/SKILL.md").read_text()
    assert "untrusted data" in text
    assert "Do not change the order returned by `rank`" in text
    assert "Do not call `send_message`" in text
    assert "3500" in text


def test_skill_requires_one_repair_attempt_for_invalid_review():
    text = (ROOT / "deploy/hermes/skills/github-daily-editor/SKILL.md").read_text()
    assert "one repair attempt" in text
    assert "report the run error" in text
