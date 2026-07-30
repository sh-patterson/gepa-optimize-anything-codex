from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


__all__ = (
    "AdapterEvidence",
    "installed_provenance",
    "read_adapter_evidence",
    "git_commit",
    "installed_vcs_commit",
    "hash_files",
    "write_verified_receipt",
)


@dataclass(frozen=True)
class AdapterEvidence:
    invocation_paths: tuple[Path, ...]
    session_paths: tuple[Path, ...]
    usage: dict[str, int]
    estimated_cost_usd: float
    mappings: tuple[dict[str, object], ...]


_REQUIRED_INVOCATION_FIELDS = frozenset(
    {
        "schema_version",
        "upstream_session_id",
        "codex_thread_id",
        "resume",
        "source_model",
        "target_model",
        "reasoning_effort",
        "return_code",
        "terminal_status",
        "usage",
        "estimated_cost_usd",
        "cost_status",
        "duration_ms",
    }
)


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {name}")
    return value


def _nonnegative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {name}")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError(f"invalid {name}")
    return numeric


def _read_invocation(path: Path, target_model: str, reasoning_effort: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid invocation JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"invalid invocation record: {path}")
    missing = _REQUIRED_INVOCATION_FIELDS - set(payload)
    if missing:
        raise ValueError(
            "missing required field in invocation record: " + ", ".join(sorted(missing))
        )
    if payload["schema_version"] != 1:
        raise ValueError("unsupported invocation schema_version")
    if payload["terminal_status"] != "completed":
        raise ValueError("invocation terminal status is not completed")
    if payload["return_code"] != 0:
        raise ValueError("invocation return code is not zero")
    if payload["target_model"] != target_model:
        raise ValueError("invocation target model does not match release policy")
    if payload["reasoning_effort"] != reasoning_effort:
        raise ValueError("invocation reasoning effort does not match release policy")
    if not isinstance(payload["upstream_session_id"], str) or not payload["upstream_session_id"]:
        raise ValueError("invalid upstream_session_id")
    if not isinstance(payload["codex_thread_id"], str) or not payload["codex_thread_id"]:
        raise ValueError("invalid codex_thread_id")
    if not isinstance(payload["resume"], bool):
        raise ValueError("invalid resume")
    if not isinstance(payload["usage"], dict):
        raise ValueError("invalid usage")
    for name in ("input_tokens", "output_tokens"):
        if name not in payload["usage"]:
            raise ValueError(f"missing required usage field: {name}")
    for name, value in payload["usage"].items():
        _nonnegative_integer(value, f"usage.{name}")
    if payload["usage"]["input_tokens"] + payload["usage"]["output_tokens"] <= 0:
        raise ValueError("invocation usage is empty")
    if _nonnegative_number(payload["estimated_cost_usd"], "estimated_cost_usd") <= 0:
        raise ValueError("invocation estimated_cost_usd is empty")
    if not isinstance(payload["cost_status"], str) or not payload["cost_status"]:
        raise ValueError("invalid cost_status")
    _nonnegative_integer(payload["duration_ms"], "duration_ms")
    return payload


def _read_session(path: Path) -> tuple[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid session mapping record") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid session mapping record")
    upstream_session_id = payload.get("upstream_session_id")
    codex_thread_id = payload.get("thread_id")
    if not isinstance(upstream_session_id, str) or not isinstance(codex_thread_id, str):
        raise ValueError("invalid session mapping record")
    return upstream_session_id, codex_thread_id


def read_adapter_evidence(
    state_dir: Path,
    *,
    target_model: str,
    reasoning_effort: str,
    expected_invocations: int | None = None,
) -> AdapterEvidence:
    invocation_dir = state_dir / "invocations"
    if not invocation_dir.is_dir():
        raise ValueError("missing invocation evidence directory")
    invocation_paths = tuple(sorted(invocation_dir.glob("*.json")))
    if not invocation_paths:
        raise ValueError("release evidence must contain at least one invocation")
    if expected_invocations is not None and len(invocation_paths) != expected_invocations:
        raise ValueError("adapter journal invocation count is inconsistent")
    records = [
        _read_invocation(path, target_model, reasoning_effort)
        for path in invocation_paths
    ]
    usage: dict[str, int] = {}
    for record in records:
        for name, value in record["usage"].items():
            usage[name] = usage.get(name, 0) + _nonnegative_integer(value, f"usage.{name}")
    estimated_cost_usd = sum(
        _nonnegative_number(record["estimated_cost_usd"], "estimated_cost_usd")
        for record in records
    )
    mappings = tuple(
        {
            "upstream_session_id": record["upstream_session_id"],
            "codex_thread_id": record["codex_thread_id"],
            "resume": record["resume"],
            "cost_status": record["cost_status"],
            "terminal_status": record["terminal_status"],
            "return_code": record["return_code"],
        }
        for record in records
    )
    session_paths = tuple(sorted((state_dir / "sessions").glob("*.json")))
    expected_pairs = {
        (mapping["upstream_session_id"], mapping["codex_thread_id"])
        for mapping in mappings
    }
    if len(session_paths) != len({mapping["upstream_session_id"] for mapping in mappings}):
        raise ValueError("adapter journal session evidence is inconsistent")
    if {_read_session(path) for path in session_paths} != expected_pairs:
        raise ValueError("adapter journal session evidence is inconsistent")
    return AdapterEvidence(
        invocation_paths=invocation_paths,
        session_paths=session_paths,
        usage=usage,
        estimated_cost_usd=estimated_cost_usd,
        mappings=mappings,
    )


def git_commit(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    commit = completed.stdout.strip()
    if completed.returncode == 0 and len(commit) == 40:
        return commit
    try:
        archived_commit = (path / "release" / "COMMIT").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot resolve git commit for {path}") from exc
    if len(archived_commit) == 40 and all(
        character in "0123456789abcdef" for character in archived_commit
    ):
        return archived_commit
    raise RuntimeError(f"cannot resolve git commit for {path}")


def installed_vcs_commit(package: str) -> str:
    raw = importlib.metadata.distribution(package).read_text("direct_url.json")
    try:
        commit = json.loads(raw or "")["vcs_info"]["commit_id"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot resolve installed {package} commit") from exc
    if not isinstance(commit, str) or len(commit) != 40:
        raise RuntimeError(f"cannot resolve installed {package} commit")
    return commit


def installed_provenance(
    skill: Path,
    repository_root: Path,
    expected_version: str,
    *,
    package: str = "gepa",
) -> dict[str, object]:
    selected = skill.expanduser().resolve()
    try:
        selected.relative_to(repository_root.resolve())
    except ValueError:
        pass
    else:
        raise RuntimeError("release proof requires an installed plugin")
    if not (selected / "SKILL.md").is_file():
        raise RuntimeError("installed skill is missing SKILL.md")
    if (selected / "scripts" / "codex_lm.py").exists():
        raise RuntimeError("installed plugin must not contain codex_lm.py")
    manifest = selected.parents[1] / ".codex-plugin" / "plugin.json"
    try:
        plugin = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("installed plugin manifest is missing or invalid") from exc
    if not isinstance(plugin, dict) or plugin.get("version") != expected_version:
        raise RuntimeError("installed plugin version does not match the runner")
    return {
        "installed_plugin": True,
        "skill_path": str(selected),
        "plugin_manifest": str(manifest),
        "plugin_version": expected_version,
        "repository_commit": git_commit(repository_root),
        "gepa_commit": installed_vcs_commit(package),
    }


def hash_files(paths: Mapping[str, Path]) -> dict[str, str]:
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise ValueError("missing required custody files: " + ", ".join(missing))
    hashes: dict[str, str] = {}
    for name, path in paths.items():
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[name] = digest.hexdigest()
    return hashes


def write_verified_receipt(path: Path, receipt: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(receipt, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    if json.loads(path.read_text(encoding="utf-8")) != receipt:
        raise RuntimeError("persisted receipt does not match the completed run")
    return path
