from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "gepa-optimize-anything-codex"


def test_skill_has_required_files_and_local_links():
    skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    assert skill_text.startswith("---\nname: gepa-optimize-anything-codex\n")
    assert "\ndescription:" in skill_text.split("---", 2)[1]
    assert (SKILL / "agents" / "openai.yaml").is_file()
    assert (SKILL / "scripts" / "claude").is_file()
    assert (SKILL / "scripts" / "codex_claude_adapter.py").is_file()
    assert (SKILL / "scripts" / "preflight.py").is_file()
    for name in (
        "api.md",
        "gotchas.md",
        "tracking.md",
        "writing_evaluators.md",
    ):
        assert (SKILL / "references" / name).is_file()


def test_repository_top_level_is_adapter_only():
    visible = {
        path.name
        for path in ROOT.iterdir()
        if path.name not in {".git", ".pytest_cache", ".ruff_cache"}
    }
    assert visible == {
        ".agents",
        ".github",
        ".gitignore",
        "LICENSE",
        "README.md",
        "UPSTREAM.md",
        "pyproject.toml",
        "tests",
    }
