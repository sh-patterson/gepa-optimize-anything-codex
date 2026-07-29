from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import fields
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_evidence.py"
SPEC = importlib.util.spec_from_file_location("release_evidence_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
evidence = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evidence
SPEC.loader.exec_module(evidence)


def _invocation(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "upstream_session_id": "gepa-session-1",
        "codex_thread_id": "codex-thread-1",
        "resume": False,
        "source_model": "gpt-5.6-luna",
        "target_model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "return_code": 0,
        "terminal_status": "completed",
        "usage": {"input_tokens": 1000, "output_tokens": 100},
        "estimated_cost_usd": 0.001735,
        "cost_status": "standard_tier_upper_estimate_from_observed_usage",
        "duration_ms": 10,
    }
    record.update(overrides)
    return record


def _write_evidence(state_dir: Path, record: dict[str, object]) -> tuple[Path, Path]:
    invocation_dir = state_dir / "invocations"
    session_dir = state_dir / "sessions"
    invocation_dir.mkdir(parents=True)
    session_dir.mkdir()
    invocation_path = invocation_dir / "one.json"
    invocation_path.write_text(json.dumps(record), encoding="utf-8")
    session_path = session_dir / "one.json"
    session_path.write_text(
        json.dumps(
            {
                "upstream_session_id": record["upstream_session_id"],
                "thread_id": record["codex_thread_id"],
            }
        ),
        encoding="utf-8",
    )
    return invocation_path, session_path


def test_public_contract_is_the_single_evidence_dataclass_and_six_helpers() -> None:
    assert evidence.__all__ == (
        "AdapterEvidence",
        "installed_provenance",
        "read_adapter_evidence",
        "git_commit",
        "installed_vcs_commit",
        "hash_files",
        "write_verified_receipt",
    )
    assert [field.name for field in fields(evidence.AdapterEvidence)] == [
        "invocation_paths",
        "session_paths",
        "usage",
        "estimated_cost_usd",
        "mappings",
    ]


def test_read_adapter_evidence_reconciles_journal_and_sessions_once(tmp_path: Path) -> None:
    invocation_path, session_path = _write_evidence(tmp_path, _invocation())

    actual = evidence.read_adapter_evidence(
        tmp_path,
        target_model="gpt-5.6-luna",
        reasoning_effort="high",
        expected_invocations=1,
    )

    assert actual.invocation_paths == (invocation_path,)
    assert actual.session_paths == (session_path,)
    assert actual.usage == {"input_tokens": 1000, "output_tokens": 100}
    assert actual.estimated_cost_usd == pytest.approx(0.001735)
    assert actual.mappings[0]["upstream_session_id"] == "gepa-session-1"

    session_path.write_text(
        json.dumps({"upstream_session_id": "gepa-session-1", "thread_id": "other"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="session evidence"):
        evidence.read_adapter_evidence(
            tmp_path,
            target_model="gpt-5.6-luna",
            reasoning_effort="high",
            expected_invocations=1,
        )


def test_hashing_and_receipt_writer_preserve_the_exact_payload(tmp_path: Path) -> None:
    custody = tmp_path / "custody.txt"
    custody.write_text("custody\n", encoding="utf-8")
    receipt = {"schema_version": 2, "status": "success", "receipt_path": "x"}
    receipt_path = tmp_path / "output" / "receipt.json"

    assert set(evidence.hash_files({"custody": custody})) == {"custody"}
    assert evidence.write_verified_receipt(receipt_path, receipt) == receipt_path
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt


def test_evidence_boundaries_fail_closed_for_schema_path_hash_and_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="missing invocation"):
        evidence.read_adapter_evidence(
            tmp_path,
            target_model="gpt-5.6-luna",
            reasoning_effort="high",
            expected_invocations=1,
        )
    _write_evidence(tmp_path, _invocation(schema_version=2))
    with pytest.raises(ValueError, match="schema_version"):
        evidence.read_adapter_evidence(
            tmp_path,
            target_model="gpt-5.6-luna",
            reasoning_effort="high",
            expected_invocations=1,
        )
    with pytest.raises(ValueError, match="missing required custody"):
        evidence.hash_files({"missing": tmp_path / "missing.txt"})
    monkeypatch.setattr(evidence.json, "loads", lambda _raw: {"changed": True})
    with pytest.raises(RuntimeError, match="persisted receipt"):
        evidence.write_verified_receipt(
            tmp_path / "output" / "receipt.json", {"schema_version": 2}
        )


def test_provenance_reads_installed_manifest_and_commit_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    skill = tmp_path / "installed" / "plugin" / "skills" / "gepa-optimize-anything-codex"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# installed\n", encoding="utf-8")
    manifest = skill.parents[1] / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"version":"1.0.0"}\n', encoding="utf-8")
    monkeypatch.setattr(evidence, "git_commit", lambda _root: "a" * 40)
    monkeypatch.setattr(evidence, "installed_vcs_commit", lambda _package: "b" * 40)

    actual = evidence.installed_provenance(skill, repository_root, "1.0.0")

    assert actual["plugin_manifest"] == str(manifest)
    assert actual["repository_commit"] == "a" * 40
    assert actual["gepa_commit"] == "b" * 40


@pytest.mark.parametrize(
    "setup, error",
    [
        ("checkout", "installed plugin"),
        ("missing_skill", "SKILL.md"),
        ("legacy_driver", "codex_lm.py"),
        ("missing_manifest", "manifest"),
        ("wrong_version", "version"),
    ],
)
def test_installed_provenance_rejects_invalid_plugin_boundaries(
    tmp_path: Path, setup: str, error: str
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    base = repository_root if setup == "checkout" else tmp_path / "installed"
    skill = base / "plugin" / "skills" / "gepa-optimize-anything-codex"
    skill.mkdir(parents=True)
    if setup != "missing_skill":
        (skill / "SKILL.md").write_text("# installed\n", encoding="utf-8")
    if setup == "legacy_driver":
        (skill / "scripts").mkdir()
        (skill / "scripts" / "codex_lm.py").write_text("# legacy\n", encoding="utf-8")
    if setup not in {"missing_manifest", "checkout", "missing_skill", "legacy_driver"}:
        manifest = skill.parents[1] / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir()
        version = "9.9.9" if setup == "wrong_version" else "1.0.0"
        manifest.write_text(json.dumps({"version": version}), encoding="utf-8")

    with pytest.raises(RuntimeError, match=error):
        evidence.installed_provenance(skill, repository_root, "1.0.0")


@pytest.mark.parametrize(
    "record, error",
    [
        ({"schema_version": 1}, "missing required field"),
        (_invocation(terminal_status="failed"), "not completed"),
        (_invocation(estimated_cost_usd="unknown"), "estimated_cost_usd"),
        (_invocation(target_model="other"), "target model"),
        (_invocation(reasoning_effort="low"), "reasoning effort"),
    ],
)
def test_adapter_evidence_rejects_invalid_invocation_records(
    tmp_path: Path, record: dict[str, object], error: str
) -> None:
    invocation_dir = tmp_path / "invocations"
    invocation_dir.mkdir()
    (invocation_dir / "one.json").write_text(json.dumps(record), encoding="utf-8")
    if "upstream_session_id" in record:
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        (session_dir / "one.json").write_text(
            json.dumps(
                {
                    "upstream_session_id": record["upstream_session_id"],
                    "thread_id": record["codex_thread_id"],
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match=error):
        evidence.read_adapter_evidence(
            tmp_path,
            target_model="gpt-5.6-luna",
            reasoning_effort="high",
            expected_invocations=1,
        )


def test_commit_helpers_read_archive_and_direct_url_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "COMMIT").write_text("a" * 40, encoding="utf-8")

    class Distribution:
        @staticmethod
        def read_text(name: str) -> str:
            assert name == "direct_url.json"
            return json.dumps({"vcs_info": {"commit_id": "b" * 40}})

    monkeypatch.setattr(
        evidence.importlib.metadata,
        "distribution",
        lambda _package: Distribution(),
    )

    assert evidence.git_commit(tmp_path) == "a" * 40
    assert evidence.installed_vcs_commit("gepa") == "b" * 40
