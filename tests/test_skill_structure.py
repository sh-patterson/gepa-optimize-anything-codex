from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "gepa-optimize-anything"
SKILL = PLUGIN / "skills" / "gepa-optimize-anything-codex"
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"


def test_skill_has_required_files_and_local_links():
    skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    agent_policy = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert skill_text.startswith("---\nname: gepa-optimize-anything-codex\n")
    assert "\ndescription:" in skill_text.split("---", 2)[1]
    assert (SKILL / "agents" / "openai.yaml").is_file()
    assert "products:\n    - CODEX" in agent_policy
    assert "allow_implicit_invocation: true" in agent_policy
    assert (SKILL / "scripts" / "claude").is_file()
    assert (SKILL / "scripts" / "codex_claude_adapter.py").is_file()
    assert (SKILL / "scripts" / "codex_lm.py").is_file()
    assert (SKILL / "scripts" / "preflight.py").is_file()
    for name in (
        "api.md",
        "gotchas.md",
        "tracking.md",
        "writing_evaluators.md",
    ):
        assert (SKILL / "references" / name).is_file()
    assert (PLUGIN / "LICENSE").is_file()
    assert (PLUGIN / "UPSTREAM.md").is_file()


def test_marketplace_points_to_the_skills_only_plugin():
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    manifest = json.loads(
        (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert marketplace["name"] == "gepa-optimize-anything-codex"
    assert marketplace["plugins"] == [
        {
            "name": "gepa-optimize-anything",
            "source": {
                "source": "local",
                "path": "./plugins/gepa-optimize-anything",
            },
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
            "category": "Engineering",
        }
    ]
    assert manifest["name"] == "gepa-optimize-anything"
    assert manifest["skills"] == "./skills/"
    assert "hooks" not in manifest
    assert "mcpServers" not in manifest
    assert "apps" not in manifest
    assert not (ROOT / ".agents" / "skills").exists()


def test_package_and_plugin_versions_match():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads(
        (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert pyproject["project"]["version"] == "0.3.1"
    assert manifest["version"] == pyproject["project"]["version"]
    assert pyproject["tool"]["setuptools"]["packages"] == []


def test_codex_agentic_caller_limits_and_adapter_default_are_distinct():
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    api = (SKILL / "references" / "api.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for document in (skill, api, readme):
        assert "Callers must explicitly set" in document
        assert "max_evals=10" in document
        assert "max_iterations=3" in document
        assert "max_candidates_per_iter=3" in document
        assert "adapter alone enforces a default of four atomic starts" in document


def test_supported_codex_cli_version_is_pinned_consistently():
    expected_install = 'npm install --prefix "$HOME/.local" @openai/codex@0.146.0'
    runtime = (
        SKILL / "scripts" / "sandbox_runtime.py"
    ).read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(
        encoding="utf-8"
    )

    assert expected_install in (ROOT / "README.md").read_text(encoding="utf-8")
    assert expected_install in (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert expected_install in runtime
    assert "@openai/codex@0.146.0" in workflow
    assert 'grep -F "0.146.0"' in workflow
