from pathlib import Path
import stat


ROOT = Path(__file__).parents[1]


def test_no_agent_wrapper_uses_hybrid_command():
    wrapper = ROOT / "deploy/hermes/github-daily-run.sh"
    text = wrapper.read_text(encoding="utf-8")
    assert " cli hybrid " in text
    assert " cli daily " not in text
    assert "reporter.py" not in text
    assert " cli rank " not in text
    assert wrapper.stat().st_mode & stat.S_IXUSR


def test_hybrid_skill_source_exists_with_required_frontmatter():
    skill = ROOT / "skills/github-daily-reporter/SKILL.md"
    assert skill.is_file()
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: github-daily-reporter" in text
    assert "description:" in text
