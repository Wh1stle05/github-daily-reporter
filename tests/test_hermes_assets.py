from pathlib import Path
import stat


ROOT = Path(__file__).parents[1]


def test_no_agent_wrapper_uses_hybrid_command():
    wrapper = ROOT / "deploy/scripts/github-daily-runner.sh"
    text = wrapper.read_text(encoding="utf-8")
    assert "cli hybrid --config" in text
    assert "cli daily" not in text
    assert "reporter.py" not in text
    assert " cli rank " not in text
    assert "export TELEGRAM_BOT_TOKEN" not in text
    assert "export TELEGRAM_CHAT_ID" not in text
    assert wrapper.stat().st_mode & stat.S_IXUSR


def test_hybrid_skill_source_exists_with_required_frontmatter():
    skill = ROOT / "skills/github-daily-reporter/SKILL.md"
    assert skill.is_file()
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: github-daily-reporter" in text
    assert "description:" in text


def test_hybrid_skill_requires_validator_report_titles():
    text = (ROOT / "skills/github-daily-reporter/SKILL.md").read_text(encoding="utf-8")
    assert "# GitHub 成长项目榜 · YYYY-MM-DD" in text
    assert "# GitHub 万星增量榜 · YYYY-MM-DD" in text
