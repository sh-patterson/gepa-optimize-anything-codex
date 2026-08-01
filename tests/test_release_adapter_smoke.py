from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_adapter_smoke.py"
SPEC = importlib.util.spec_from_file_location("release_adapter_smoke_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


def _installed_skill(tmp_path: Path) -> Path:
    plugin = tmp_path / "installed" / "gepa-optimize-anything"
    skill = plugin / "skills" / "gepa-optimize-anything-codex"
    (skill / "scripts").mkdir(parents=True)
    (plugin / ".codex-plugin").mkdir()
    for name in ("SKILL.md", "scripts/claude", "scripts/codex_claude_adapter.py", "scripts/sandbox_runtime.py"):
        path = skill / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"version": smoke._project_version()}), encoding="utf-8"
    )
    return skill


def test_cli_requires_explicit_live_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("RUN_CODEX_LIVE", raising=False)
    assert (
        smoke.main(
            ["--output-dir", str(tmp_path / "out"), "--expected-commit", "a" * 40]
        )
        == 2
    )
    assert "RUN_CODEX_LIVE=1" in capsys.readouterr().err


def test_payload_requires_exact_success() -> None:
    completed = subprocess.CompletedProcess([], 0, '{"subtype":"success","result":"ok"}', "")
    assert smoke._validate_payload(completed)["result"] == "ok"
    for return_code, payload in (
        (1, '{"subtype":"success","result":"ok"}'),
        (0, '{"subtype":"error","result":"ok"}'),
        (0, '{"subtype":"success","result":"almost"}'),
        (0, "not-json"),
    ):
        with pytest.raises(RuntimeError):
            smoke._validate_payload(
                subprocess.CompletedProcess([], return_code, payload, "")
            )


def test_raw_terminal_requires_ordered_completed_jsonl(
    tmp_path: Path,
) -> None:
    path = tmp_path / "terminal.jsonl"
    records = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "ok"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    assert smoke._raw_terminal(path) == {
        "thread_id": "thread-1",
        "message": "ok",
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }
    path.write_text(
        "\n".join(json.dumps(record) for record in reversed(records)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="sequence"):
        smoke._raw_terminal(path)


def test_installed_provenance_binds_files_to_exact_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _installed_skill(tmp_path)
    plugin = skill.parents[1]
    expected = "b" * 40
    monkeypatch.setattr(smoke, "_git_head", lambda: expected)
    blobs = {
        repository_name: (plugin / installed_name).read_bytes()
        for installed_name, repository_name in smoke.PROVENANCE_FILES.items()
    }
    monkeypatch.setattr(smoke, "_git_blob", lambda commit, path: blobs[path])
    actual = smoke.installed_adapter_provenance(skill, expected)
    assert actual["installed_plugin"] is True
    assert actual["repository_commit"] == expected
    assert len(actual["installed_file_sha256"]) == len(smoke.PROVENANCE_FILES)

    first = next(iter(blobs))
    blobs[first] = b"changed"
    with pytest.raises(RuntimeError, match="does not match"):
        smoke.installed_adapter_provenance(skill, expected)


def test_provenance_rejects_source_checkout_and_wrong_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_skill = (
        ROOT
        / "plugins"
        / "gepa-optimize-anything"
        / "skills"
        / "gepa-optimize-anything-codex"
    )
    monkeypatch.setattr(smoke, "_git_head", lambda: "c" * 40)
    with pytest.raises(RuntimeError, match="installed plugin"):
        smoke.installed_adapter_provenance(source_skill, "c" * 40)
    with pytest.raises(RuntimeError, match="expected commit"):
        smoke.installed_adapter_provenance(source_skill, "d" * 40)


def test_smoke_contract_is_luna_only_and_excludes_optimizer_layers() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert smoke.TARGET_MODEL == "gpt-5.6-luna"
    assert smoke.REASONING_EFFORT == "high"
    assert "gpt-5.6-sol" not in source
    assert "stage_runtime(" not in source
    assert 'str(skill / "scripts" / "claude")' in source
    for exclusion in (
        "gepa_optimizer",
        "optimize_anything",
        "evaluator",
        "judge",
        "research_bullet_harness",
    ):
        assert exclusion in source


def test_smoke_runs_installed_adapter_and_conserves_fake_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _installed_skill(tmp_path)
    source_skill = (
        ROOT
        / "plugins"
        / "gepa-optimize-anything"
        / "skills"
        / "gepa-optimize-anything-codex"
    )
    for name in (
        "SKILL.md",
        "scripts/claude",
        "scripts/codex_claude_adapter.py",
        "scripts/sandbox_runtime.py",
    ):
        shutil.copyfile(source_skill / name, skill / name)
    (skill / "scripts" / "claude").chmod(0o755)
    home = tmp_path / "home"
    codex = home / ".local" / "bin" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text(
        f"""#!{sys.executable}
import json

for record in (
    {{"type": "thread.started", "thread_id": "thread-1"}},
    {{"type": "turn.started"}},
    {{"type": "item.completed", "item": {{"type": "agent_message", "text": "ok"}}}},
    {{"type": "turn.completed", "usage": {{"input_tokens": 2, "output_tokens": 1}}}},
):
    print(json.dumps(record))
""",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    expected = "e" * 40
    plugin = skill.parents[1]
    blobs = {
        repository_name: (plugin / installed_name).read_bytes()
        for installed_name, repository_name in smoke.PROVENANCE_FILES.items()
    }
    monkeypatch.setattr(smoke, "_git_head", lambda: expected)
    monkeypatch.setattr(smoke, "_git_blob", lambda commit, path: blobs[path])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GEPA_CODEX_SKILL_DIR", str(skill))
    monkeypatch.setenv("CODEX_CLI", str(codex))
    for name in smoke.API_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)

    output = tmp_path / "output"
    receipt = smoke.run_smoke(output, expected)

    assert receipt["status"] == "success"
    assert receipt["scope"] == "adapter_only"
    assert receipt["terminal"] == {
        "return_code": 0,
        "subtype": "success",
        "result": "ok",
        "raw_jsonl_reconciled": True,
    }
    assert receipt["usage"] == {"input_tokens": 2, "output_tokens": 1}
    assert receipt["session_mapping"]["resume"] is False
    assert (output / "codex-terminal.jsonl").is_file()
    assert json.loads(
        (output / "adapter-smoke-receipt.json").read_text(encoding="utf-8")
    ) == receipt
