from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from hashlib import sha256
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "installed_skill_dogfood.py"
FIXTURES = ROOT / "release" / "fixtures"
SPEC = importlib.util.spec_from_file_location("installed_skill_dogfood_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
dogfood = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dogfood
SPEC.loader.exec_module(dogfood)


def _result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "success",
        "seed_score": 0.125,
        "best_score": 0.875,
        "best_candidate_sha256": "a" * 64,
        "best_candidate_valid_json": True,
        "total_evals": 3,
        "candidate_hashes": ["b" * 64, "c" * 64],
        "score_trace": [
            {"candidate_sha256": "d" * 64, "score": 0.125, "valid_json": True},
            {"candidate_sha256": "b" * 64, "score": 0.5, "valid_json": True},
            {"candidate_sha256": "c" * 64, "score": 0.875, "valid_json": True},
        ],
    }
    result.update(overrides)
    return result


def _installed_skill(tmp_path: Path) -> Path:
    plugin = tmp_path / "installed" / "gepa-optimize-anything"
    skill = plugin / "skills" / "gepa-optimize-anything-codex"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# installed\n", encoding="utf-8")
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"version":"1.0.1"}\n', encoding="utf-8")
    (skill / "scripts").mkdir()
    return skill


def _load_task(path: Path) -> object:
    spec = importlib.util.spec_from_file_location("support_routing_task", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _provenance(skill: Path, *_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "installed_plugin": True,
        "skill_path": str(skill),
        "plugin_manifest": str(skill.parents[1] / ".codex-plugin" / "plugin.json"),
        "plugin_version": "1.0.1",
        "repository_commit": "a" * 40,
        "gepa_commit": "b" * 40,
    }


def test_canonical_routing_task_has_eight_cases_and_seed_passes_only_fallback() -> None:
    task = _load_task(FIXTURES / "support_routing_task.py")
    cases = dogfood._task_fixture(FIXTURES / "support_routing_cases.json")

    seed_score, failed, valid = task.score_candidate(dogfood.SEED, cases)
    assert valid is True
    assert seed_score == 0.125
    assert len(failed) == 7

    winner = json.dumps(
        {
            "routes": [
                {"keywords": ["charged", "invoice"], "destination": "billing"},
                {"keywords": ["refund", "cancel"], "destination": "support"},
                {
                    "keywords": ["password", "login code"],
                    "destination": "account_security",
                },
                {"keywords": ["crash", "outage"], "destination": "technical"},
            ],
            "default": "general",
        }
    )
    score, failed, valid = task.score_candidate(winner, cases)
    assert valid is True
    assert score == 1.0
    assert failed == []


def test_fixture_copies_match_canonical_sources_byte_for_byte(tmp_path: Path) -> None:
    fixture, task = dogfood._write_fixture(tmp_path)

    assert fixture.read_bytes() == dogfood.ROUTING_CASES_FIXTURE.read_bytes()
    assert task.read_bytes() == dogfood.TASK_FIXTURE.read_bytes()


def test_runner_has_no_embedded_fixture_constants() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert not hasattr(dogfood, "ROUTING_CASES")
    assert not hasattr(dogfood, "TASK_SOURCE")
    assert "ROUTING_CASES =" not in source
    assert "TASK_SOURCE" not in source


@pytest.mark.parametrize(
    "result, error",
    [
        (_result(best_candidate_valid_json=False), "valid JSON"),
        (_result(candidate_hashes=["b" * 64, "b" * 64]), "two distinct"),
        (_result(best_score=0.5), "required score"),
        (_result(score_trace=[]), "score trace"),
    ],
)
def test_result_validation_fails_closed(result: dict[str, object], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        dogfood._validate_result(result)


def test_cli_requires_explicit_live_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("RUN_CODEX_LIVE", raising=False)

    assert dogfood.main(["--output-dir", str(tmp_path / "output")]) == 2
    assert "RUN_CODEX_LIVE=1" in capsys.readouterr().err


def test_outer_codex_allows_local_eval_socket_without_web_search(
    tmp_path: Path,
) -> None:
    runtime_cache = tmp_path / "runtime-cache"
    command = dogfood._outer_command(
        "codex", tmp_path, tmp_path / "task.py", runtime_cache
    )

    assert "sandbox_workspace_write.network_access=true" in command
    assert (
        "sandbox_workspace_write.writable_roots="
        + json.dumps([str(runtime_cache)])
        in command
    )
    assert "features.standalone_web_search=false" in command
    assert command[command.index("--sandbox") + 1] == "workspace-write"


@pytest.mark.skipif(os.name != "posix", reason="fake Codex executable requires POSIX")
def test_outer_codex_rejects_missing_usage_and_terminal_ambiguity(
    tmp_path: Path,
) -> None:
    adapter_path = (
        ROOT
        / "plugins"
        / "gepa-optimize-anything"
        / "skills"
        / "gepa-optimize-anything-codex"
        / "scripts"
        / "codex_claude_adapter.py"
    )
    adapter = dogfood._load_module("dogfood_test_adapter", adapter_path)
    fake = tmp_path / "codex"

    for events, error in (
        (
            [
                {"type": "thread.started", "thread_id": "outer-thread"},
                {"type": "turn.completed"},
            ],
            "did not complete terminally",
        ),
        (
            [
                {"type": "thread.started", "thread_id": "outer-thread"},
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            ],
            "did not complete terminally",
        ),
    ):
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json\n"
            f"events = {events!r}\n"
            "for event in events:\n"
            "    print(json.dumps(event))\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        with pytest.raises(RuntimeError, match=error):
            dogfood._run_outer_codex([str(fake)], dict(os.environ), adapter)


@pytest.mark.skipif(os.name != "posix", reason="fake Codex executable requires POSIX")
def test_fake_codex_run_writes_sanitized_hashed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skill = _installed_skill(tmp_path)
    fake = tmp_path / "fake-codex"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        "from pathlib import Path\n"
        f"result = {_result()!r}\n"
        "Path(os.environ['DOGFOOD_RESULT_PATH']).write_text("
        "json.dumps(result), encoding='utf-8')\n"
        "state = Path(os.environ['CODEX_ADAPTER_STATE_DIR'])\n"
        "(state / 'invocations').mkdir(parents=True, exist_ok=True)\n"
        "(state / 'sessions').mkdir(parents=True, exist_ok=True)\n"
        "record = {\n"
        "  'schema_version': 1,\n"
        "  'upstream_session_id': 'inner-session',\n"
        "  'codex_thread_id': 'inner-thread',\n"
        "  'resume': False,\n"
        "  'source_model': 'gpt-5.6-luna',\n"
        "  'target_model': 'gpt-5.6-luna',\n"
        "  'reasoning_effort': 'high',\n"
        "  'return_code': 0,\n"
        "  'terminal_status': 'completed',\n"
        "  'usage': {'input_tokens': 10, 'output_tokens': 2},\n"
        "  'estimated_cost_usd': 0.001,\n"
        "  'cost_status': 'standard_tier_upper_estimate_from_observed_usage',\n"
        "  'duration_ms': 1,\n"
        "}\n"
        "(state / 'invocations' / 'one.json').write_text("
        "json.dumps(record), encoding='utf-8')\n"
        "(state / 'sessions' / 'one.json').write_text(json.dumps({"
        "'upstream_session_id': 'inner-session', 'thread_id': 'inner-thread'"
        "}), encoding='utf-8')\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'outer-thread'}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {"
        "'input_tokens': 20, 'output_tokens': 3}}))\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    adapter_path = (
        ROOT
        / "plugins"
        / "gepa-optimize-anything"
        / "skills"
        / "gepa-optimize-anything-codex"
        / "scripts"
        / "codex_claude_adapter.py"
    )
    adapter = dogfood._load_module("dogfood_success_adapter", adapter_path)
    monkeypatch.setattr(dogfood, "installed_skill_path", lambda: skill)
    monkeypatch.setattr(
        dogfood,
        "stage_and_preflight",
        lambda _skill, _engine, state_dir: (
            {
                **os.environ,
                "CODEX_CLI": str(fake),
                "CODEX_ADAPTER_STATE_DIR": str(state_dir),
                "CODEX_ADAPTER_AUTH_MODE": "chatgpt_login",
                "CODEX_ADAPTER_MAX_INVOCATIONS": "1",
                "CODEX_ADAPTER_PRE_SUBMISSION_RETRIES": "0",
            },
            {
                "probe_success": True,
                "sandbox_runtime": "bubblewrap",
                "state_dir": str(state_dir),
            },
        ),
    )
    monkeypatch.setattr(dogfood, "_load_module", lambda _name, _path: adapter)
    monkeypatch.setattr(
        dogfood,
        "installed_provenance",
        _provenance,
    )

    receipt = dogfood.run_dogfood(tmp_path / "output")

    assert receipt["status"] == "success"
    assert receipt["schema_version"] == 2
    assert receipt["result"]["best_score"] == 0.875
    assert receipt["usage"] == {
        "outer_codex": {"input_tokens": 20, "output_tokens": 3},
        "optimizer_codex": {"input_tokens": 10, "output_tokens": 2},
    }
    assert receipt["cost"]["optimizer_estimated_usd"] == pytest.approx(0.001)
    assert receipt["terminal"]["outer"]["thread_id"].startswith("sha256:")
    assert receipt["session_mapping"]["upstream_session_id"].startswith("sha256:")
    assert receipt["session_mapping"]["codex_thread_id"].startswith("sha256:")
    assert receipt["terminal"]["optimizer"]["ambiguous_retry"] is False
    assert set(receipt["hashes"]) == {
        "plugin_manifest",
        "skill",
        "runner",
        "task_runner",
        "evaluator_fixture",
        "invocation",
        "outer_terminal",
        "session",
    }
    assert receipt["hashes"]["task_runner"] == sha256(
        (tmp_path / "output" / "support_routing_task.py").read_bytes()
    ).hexdigest()
    assert receipt["hashes"]["evaluator_fixture"] == sha256(
        (tmp_path / "output" / "support_routing_cases.json").read_bytes()
    ).hexdigest()
    serialized = json.dumps(receipt)
    assert "Use $gepa" not in serialized
    assert "routes" not in serialized
    assert "API_KEY" not in serialized
    assert "outer-thread" not in serialized
    assert "inner-session" not in serialized
    assert "inner-thread" not in serialized
    assert str(tmp_path) not in serialized
    assert receipt["receipt_path"] == "$HOME/output/installed-skill-dogfood-receipt.json"
    persisted = tmp_path / "output" / "installed-skill-dogfood-receipt.json"
    assert json.loads(persisted.read_text(encoding="utf-8")) == receipt


def test_runner_rejects_wrong_provenance_and_retry_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        dogfood,
        "installed_skill_path",
        lambda: (_ for _ in ()).throw(RuntimeError("requires an installed plugin")),
    )
    with pytest.raises(RuntimeError, match="installed plugin"):
        dogfood.run_dogfood(tmp_path / "wrong-provenance")

    skill = _installed_skill(tmp_path / "retry")
    monkeypatch.setattr(dogfood, "installed_skill_path", lambda: skill)
    monkeypatch.setattr(dogfood, "installed_provenance", _provenance)
    monkeypatch.setattr(
        dogfood,
        "stage_and_preflight",
        lambda _skill, _engine, state_dir: (
            {
                "CODEX_ADAPTER_AUTH_MODE": "chatgpt_login",
                "CODEX_ADAPTER_MAX_INVOCATIONS": "1",
                "CODEX_ADAPTER_PRE_SUBMISSION_RETRIES": "1",
            },
            {"probe_success": True},
        ),
    )
    with pytest.raises(ValueError, match="not fail-closed"):
        dogfood.run_dogfood(tmp_path / "retry-leakage")


def test_runner_rejects_fabricated_result_before_result_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _installed_skill(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(dogfood, "installed_skill_path", lambda: skill)
    monkeypatch.setattr(dogfood, "installed_provenance", _provenance)
    monkeypatch.setattr(
        dogfood,
        "stage_and_preflight",
        lambda _skill, _engine, state_dir: (
            {
                **os.environ,
                "CODEX_CLI": "unused",
                "CODEX_ADAPTER_STATE_DIR": str(state_dir),
                "CODEX_ADAPTER_AUTH_MODE": "chatgpt_login",
                "CODEX_ADAPTER_MAX_INVOCATIONS": "1",
                "CODEX_ADAPTER_PRE_SUBMISSION_RETRIES": "0",
            },
            {"probe_success": True},
        ),
    )
    monkeypatch.setattr(dogfood, "_load_module", lambda _name, _path: object())

    def fabricate_result(
        _command: list[str], environment: dict[str, str], _adapter: object
    ) -> dict[str, object]:
        Path(environment["DOGFOOD_RESULT_PATH"]).write_text(
            json.dumps(_result(seed_score=0.0)), encoding="utf-8"
        )
        return {
            "thread_id": "outer-thread",
            "terminal_status": "completed",
            "return_code": 0,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "estimated_cost_usd": 0.0,
        }

    monkeypatch.setattr(
        dogfood,
        "_run_outer_codex",
        fabricate_result,
    )
    output = tmp_path / "fabricated"

    with pytest.raises(ValueError, match="missing invocation evidence"):
        dogfood.run_dogfood(output)
