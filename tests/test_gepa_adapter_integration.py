from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / ".agents" / "skills" / "gepa-optimize-anything-codex" / "scripts"


@pytest.mark.skipif(os.name != "posix", reason="the compatibility command is Linux-only")
def test_pinned_gepa_autoresearch_edits_evaluates_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("gepa")
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    fake_codex = tmp_path / "codex"
    invocation_log = tmp_path / "codex-invocations.jsonl"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
resumed = len(args) > 1 and args[0:2] == ["exec", "resume"]
candidate = Path("best_candidate.txt")
candidate.write_text("Return BLUE." if resumed else "Return RED again.", encoding="utf-8")
subprocess.run(["./eval.sh", str(candidate)], check=False)
with Path(os.environ["FAKE_CODEX_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
if not resumed:
    print(json.dumps({"type": "thread.started", "thread_id": "codex-thread-1"}))
print(json.dumps({
    "type": "item.completed",
    "item": {"type": "agent_message", "text": "candidate evaluated"},
}))
print(json.dumps({
    "type": "turn.completed",
    "usage": {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 100},
}))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("CODEX_CLI", str(fake_codex))
    monkeypatch.setenv("CODEX_ADAPTER_STATE_DIR", str(tmp_path / "adapter-state"))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(invocation_log))
    monkeypatch.setenv("PATH", f"{SCRIPTS}{os.pathsep}{os.environ['PATH']}")

    def evaluate(candidate: str, _example: object) -> tuple[float, dict]:
        passed = "BLUE" in candidate
        return float(passed), {
            "feedback": "Candidate must contain BLUE.",
            "passed": passed,
        }

    result = optimize_anything(
        seed_candidate="Return RED.",
        evaluator=evaluate,
        dataset=[{"case": "exact-token"}],
        objective="Write a candidate containing BLUE.",
        config=OptimizeAnythingConfig(
            engine="autoresearch",
            max_evals=5,
            stop_at_score=1.0,
            sandbox=False,
            run_dir=str(tmp_path / "run"),
            output_dir=str(tmp_path / "output"),
            engine_config={"ralph": True},
        ),
    )

    invocations = [
        json.loads(line)
        for line in invocation_log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(invocations) == 2
    assert "resume" not in invocations[0]
    assert invocations[1][0:2] == ["exec", "resume"]
    assert "codex-thread-1" in invocations[1]
    assert result.best_candidate == "Return BLUE."
    assert result.best_score == 1.0
    assert result.total_evals == 2
    assert result.metadata["adapter_cost"] > 0
    assert len(list((tmp_path / "adapter-state" / "sessions").glob("*.json"))) == 1


@pytest.mark.skipif(os.name != "posix", reason="the compatibility command is Linux-only")
def test_pinned_gepa_meta_harness_reads_skill_and_submits_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("gepa")
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    fake_codex = tmp_path / "codex"
    invocation_log = tmp_path / "codex-invocations.jsonl"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
prompt = args[-1]
skill = Path(".claude/skills/gepa-optimize-anything-meta-harness/SKILL.md")
assert skill.exists()
assert "Write pending_eval.json" in skill.read_text(encoding="utf-8")
assert "Follow the gepa-optimize-anything-meta-harness skill" in prompt
candidate = Path("agents/iter1_blue.txt")
candidate.write_text("Return BLUE.", encoding="utf-8")
pending = Path("state/pending_eval_iter1.json")
pending.write_text(json.dumps({
    "iteration": 1,
    "candidates": [{
        "name": "blue",
        "file": str(candidate),
        "hypothesis": "The required token will pass.",
        "axis": "exploitation",
        "base": "baseline",
        "components": ["required-token"],
    }],
}), encoding="utf-8")
with Path(os.environ["FAKE_CODEX_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
print(json.dumps({"type": "thread.started", "thread_id": "codex-thread-mh-1"}))
print(json.dumps({
    "type": "item.completed",
    "item": {"type": "agent_message", "text": "CANDIDATES: blue"},
}))
print(json.dumps({
    "type": "turn.completed",
    "usage": {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 100},
}))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("CODEX_CLI", str(fake_codex))
    monkeypatch.setenv("CODEX_ADAPTER_STATE_DIR", str(tmp_path / "adapter-state"))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(invocation_log))
    monkeypatch.setenv("PATH", f"{SCRIPTS}{os.pathsep}{os.environ['PATH']}")

    def evaluate(candidate: str) -> tuple[float, dict]:
        passed = "BLUE" in candidate
        return float(passed), {
            "feedback": "Candidate must contain BLUE.",
            "passed": passed,
        }

    result = optimize_anything(
        seed_candidate="Return RED.",
        evaluator=evaluate,
        objective="Write a candidate containing BLUE.",
        config=OptimizeAnythingConfig(
            engine="meta_harness",
            max_evals=3,
            stop_at_score=1.0,
            sandbox=False,
            run_dir=str(tmp_path / "run"),
            output_dir=str(tmp_path / "output"),
            engine_config={
                "max_iterations": 1,
                "max_candidates_per_iter": 1,
            },
        ),
    )

    invocations = [
        json.loads(line)
        for line in invocation_log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(invocations) == 1
    assert "resume" not in invocations[0]
    assert result.best_candidate == "Return BLUE."
    assert result.best_score == 1.0
    assert result.total_evals == 2
    assert result.metadata["adapter_cost"] > 0
    assert result.metadata["meta_harness"]["stop_reason"] == "perfect_score"
    assert len(list((tmp_path / "adapter-state" / "sessions").glob("*.json"))) == 1
