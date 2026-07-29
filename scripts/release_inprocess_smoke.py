from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any, Callable


ENGINES = frozenset({"gepa", "best_of_n"})
MODEL = "openai/gpt-5.1"
SEED = "Return RED."
TARGET = "Return BLUE."
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    commit = completed.stdout.strip()
    if completed.returncode != 0 or len(commit) != 40:
        raise RuntimeError("cannot resolve release runner commit")
    return commit


def _installed_vcs_commit(package: str) -> str:
    raw = importlib.metadata.distribution(package).read_text("direct_url.json")
    try:
        payload = json.loads(raw or "")
        commit = payload["vcs_info"]["commit_id"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot resolve installed {package} commit") from exc
    if not isinstance(commit, str) or len(commit) != 40:
        raise RuntimeError(f"cannot resolve installed {package} commit")
    return commit


def _installed_skill() -> tuple[Path, Path]:
    configured = os.environ.get("GEPA_CODEX_SKILL_DIR")
    if not configured:
        raise RuntimeError("in-process smoke requires GEPA_CODEX_SKILL_DIR")
    skill = Path(configured).expanduser().resolve()
    try:
        skill.relative_to(REPOSITORY_ROOT)
    except ValueError:
        pass
    else:
        raise RuntimeError("in-process smoke requires an installed plugin")
    if not (skill / "SKILL.md").is_file():
        raise RuntimeError("installed skill is missing SKILL.md")
    manifest = skill.parents[1] / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("installed plugin manifest is missing or invalid") from exc
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as source:
        expected_version = tomllib.load(source)["project"]["version"]
    if not isinstance(payload, dict) or payload.get("version") != expected_version:
        raise RuntimeError("installed plugin version does not match the runner")
    return skill, manifest


def _evaluate(candidate: str, _example: object = None) -> tuple[float, dict[str, str]]:
    passed = TARGET in candidate
    return float(passed), {"feedback": "Candidate must contain BLUE."}


def config_for(engine: str, root: Path) -> dict[str, Any]:
    if engine not in ENGINES:
        raise ValueError(f"unsupported engine: {engine}")
    engine_config: dict[str, Any] = {
        "model": MODEL,
        "max_n": 3,
        "lm_kwargs": {"num_retries": 0},
    }
    if engine == "gepa":
        engine_config = {
            "reflection": {
                "reflection_lm": MODEL,
                "reflection_lm_kwargs": {"num_retries": 0},
            },
            "engine": {},
        }
    return {
        "engine": engine,
        "max_evals": 4,
        "stop_at_score": 1.0,
        "run_dir": str(root / "run"),
        "output_dir": str(root / "output"),
        "engine_config": engine_config,
    }


def _usage(metadata: object) -> dict[str, int]:
    if not isinstance(metadata, dict):
        raise RuntimeError("optimizer result metadata is missing")
    value = metadata.get("usage", metadata)
    if not isinstance(value, dict):
        raise RuntimeError("optimizer result usage is missing")
    usage: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens"):
        token_count = value.get(name)
        if isinstance(token_count, bool) or not isinstance(token_count, int):
            raise RuntimeError(f"optimizer usage is missing {name}")
        usage[name] = token_count
    if sum(usage.values()) <= 0:
        raise RuntimeError("optimizer usage is empty")
    return usage


def _native_usage(lms: list[object]) -> dict[str, int]:
    usage = {
        "input_tokens": sum(int(getattr(lm, "total_tokens_in", 0)) for lm in lms),
        "output_tokens": sum(int(getattr(lm, "total_tokens_out", 0)) for lm in lms),
    }
    if sum(usage.values()) <= 0:
        raise RuntimeError("optimizer usage is empty")
    return usage


def _result(result: object) -> dict[str, Any]:
    for name in ("best_candidate", "best_score", "total_evals", "metadata"):
        if not hasattr(result, name):
            raise RuntimeError(f"optimizer result is missing {name}")
    candidate = getattr(result, "best_candidate")
    score = getattr(result, "best_score")
    evals = getattr(result, "total_evals")
    if (
        candidate != TARGET
        or not isinstance(score, (int, float))
        or score != 1.0
        or isinstance(evals, bool)
        or not isinstance(evals, int)
        or evals <= 0
    ):
        raise RuntimeError("optimizer did not complete RED to BLUE")
    return {
        "improved": True,
        "best_score": float(score),
        "eval_count": int(evals),
        "best_candidate_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
    }


def _atomic_receipt(path: Path, receipt: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            json.dump(receipt, target, indent=2, sort_keys=True)
            target.write("\n")
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _run_native(engine: str, values: dict[str, Any]) -> tuple[object, dict[str, int]]:
    from gepa.lm import LM
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    lms: list[object] = []
    if engine == "gepa":
        lm = LM(MODEL, num_retries=0)
        lms.append(lm)
        reflection = values["engine_config"]["reflection"]
        reflection["reflection_lm"] = lm
        reflection.pop("reflection_lm_kwargs")
        raw = optimize_anything(
            seed_candidate=SEED,
            evaluator=_evaluate,
            objective="Write a candidate containing BLUE.",
            config=OptimizeAnythingConfig(**values),
        )
    else:
        import gepa.oa.engines.best_of_n as best_of_n

        native_lm = best_of_n.LM

        def recording_lm(model: str, **kwargs: Any) -> object:
            if model != MODEL:
                raise RuntimeError("optimizer selected an unexpected model")
            lm = native_lm(model, **kwargs)
            lms.append(lm)
            return lm

        best_of_n.LM = recording_lm
        try:
            raw = optimize_anything(
                seed_candidate=SEED,
                evaluator=_evaluate,
                objective="Write a candidate containing BLUE.",
                config=OptimizeAnythingConfig(**values),
            )
        finally:
            best_of_n.LM = native_lm
    return raw, _native_usage(lms)


def run_smoke(
    engine: str,
    output_dir: Path,
    *,
    optimize: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("in-process smoke requires OPENAI_API_KEY")
    skill, manifest = _installed_skill()
    output_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    values = config_for(engine, output_dir)
    if optimize is None:
        raw, usage = _run_native(engine, values)
    else:
        from gepa.optimize_anything import OptimizeAnythingConfig

        raw = optimize(
            seed_candidate=SEED,
            evaluator=_evaluate,
            objective="Write a candidate containing BLUE.",
            config=OptimizeAnythingConfig(**values),
        )
        usage = _usage(getattr(raw, "metadata", None))
    result = _result(raw)
    duration_ms = round((time.monotonic() - started) * 1000)
    source_files = {
        "skill": skill / "SKILL.md",
        "plugin_manifest": manifest,
        "runner": Path(__file__).resolve(),
    }
    receipt = {
        "schema_version": 1,
        "status": "success",
        "engine": engine,
        "provider": "openai",
        "model": MODEL,
        "retry_count": 0,
        "result": result,
        "usage": usage,
        "duration_ms": duration_ms,
        "provenance": {
            "installed_plugin": True,
            "skill_path": str(skill),
            "plugin_manifest": str(manifest),
            "plugin_version": json.loads(manifest.read_text(encoding="utf-8"))[
                "version"
            ],
            "repository_commit": _git_commit(REPOSITORY_ROOT),
            "gepa_commit": _installed_vcs_commit("gepa"),
        },
        "hashes": {name: _sha256(path) for name, path in source_files.items()},
    }
    receipt_path = output_dir / f"{engine}-receipt.json"
    receipt["receipt_path"] = str(receipt_path)
    _atomic_receipt(receipt_path, receipt)
    if json.loads(receipt_path.read_text(encoding="utf-8")) != receipt:
        raise RuntimeError("persisted receipt does not match the completed run")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=sorted(ENGINES), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.environ.get("RUN_CODEX_LIVE") != "1":
        print("in-process smoke requires RUN_CODEX_LIVE=1", file=sys.stderr)
        return 2
    print(json.dumps(run_smoke(args.engine, args.output_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
