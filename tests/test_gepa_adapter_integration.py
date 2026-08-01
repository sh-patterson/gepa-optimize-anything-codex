from __future__ import annotations

import json
import os
import shutil
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
CHECKOUT_SKILL_DIR = (
    ROOT
    / "plugins"
    / "gepa-optimize-anything"
    / "skills"
    / "gepa-optimize-anything-codex"
)
SKILL_DIR = Path(os.environ.get("GEPA_CODEX_SKILL_DIR", CHECKOUT_SKILL_DIR)).resolve()
assert (SKILL_DIR / "SKILL.md").is_file()
SCRIPTS = SKILL_DIR / "scripts"
RUNTIME_SPEC = importlib.util.spec_from_file_location(
    "sandbox_runtime", SCRIPTS / "sandbox_runtime.py"
)
assert RUNTIME_SPEC is not None and RUNTIME_SPEC.loader is not None
sandbox_runtime = importlib.util.module_from_spec(RUNTIME_SPEC)
sys.modules[RUNTIME_SPEC.name] = sandbox_runtime
RUNTIME_SPEC.loader.exec_module(sandbox_runtime)


def _sandboxed_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Path
) -> Path:
    fake_home = tmp_path / "home"
    state_dir = fake_home / ".cache" / "gepa-optimize-anything-codex" / "runs" / "test"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CODEX_CLI", str(fake_codex))
    monkeypatch.setenv("CODEX_ADAPTER_STATE_DIR", str(state_dir))
    paths = sandbox_runtime.stage_runtime(
        sandbox_runtime.runtime_paths(home=fake_home)
    )
    state_dir = sandbox_runtime.resolve_state_dir(paths, state_dir)
    for name, value in sandbox_runtime.runtime_environment(paths, state_dir).items():
        monkeypatch.setenv(name, value)
    return state_dir


@pytest.mark.parametrize("engine", ("autoresearch", "meta_harness"))
@pytest.mark.skipif(
    os.name != "posix" or not shutil.which("bwrap"),
    reason="the sandboxed compatibility command requires Linux bubblewrap",
)
def test_agentic_max_token_cost_rejects_before_codex_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    pytest.importorskip("gepa")
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    fake_codex = tmp_path / "home" / ".local" / "bin" / "codex"
    fake_codex.parent.mkdir(parents=True)
    marker = tmp_path / "codex-started"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started', encoding='utf-8')\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    _sandboxed_runtime(tmp_path, monkeypatch, fake_codex)

    config = OptimizeAnythingConfig(
        engine=engine,
        max_evals=1,
        max_token_cost=0.01,
        sandbox=True,
        run_dir=str(tmp_path / "run"),
        output_dir=str(tmp_path / "output"),
        engine_config=(
            {"ralph": True}
            if engine == "autoresearch"
            else {"max_iterations": 1, "max_candidates_per_iter": 1}
        ),
    )
    if engine == "autoresearch":
        with pytest.raises(RuntimeError, match="max-budget-usd"):
            optimize_anything(
                seed_candidate="Return RED.",
                evaluator=lambda candidate, _example=None: (
                    0.0,
                    {"feedback": candidate},
                ),
                objective="Write a candidate containing BLUE.",
                config=config,
            )
    else:
        result = optimize_anything(
            seed_candidate="Return RED.",
            evaluator=lambda candidate, _example=None: (
                0.0,
                {"feedback": candidate},
            ),
            objective="Write a candidate containing BLUE.",
            config=config,
        )
        assert result.metadata["meta_harness"]["stop_reason"] == "proposer_failed"
    assert not marker.exists()


@pytest.mark.skipif(
    os.name != "posix" or not shutil.which("bwrap"),
    reason="the sandboxed compatibility command requires Linux bubblewrap",
)
def test_pinned_gepa_autoresearch_edits_evaluates_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("gepa")
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    fake_codex = tmp_path / "home" / ".local" / "bin" / "codex"
    fake_codex.parent.mkdir(parents=True)
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
with (Path(os.environ["CODEX_ADAPTER_STATE_DIR"]) / "codex-invocations.jsonl").open("a", encoding="utf-8") as log:
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
    state_dir = _sandboxed_runtime(tmp_path, monkeypatch, fake_codex)
    invocation_log = state_dir / "codex-invocations.jsonl"

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
            max_evals=10,
            stop_at_score=1.0,
            sandbox=True,
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
    assert all("--max-budget-usd" not in invocation for invocation in invocations)
    assert result.best_candidate == "Return BLUE."
    assert result.best_score == 1.0
    assert result.total_evals == 2
    assert len(list((state_dir / "sessions").glob("*.json"))) == 1
    invocation_records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (state_dir / "invocations").glob("*.json")
    ]
    assert len(invocation_records) == 2
    assert {record["terminal_status"] for record in invocation_records} == {"completed"}
    assert {record["resume"] for record in invocation_records} == {False, True}
    assert all(record["usage"]["input_tokens"] > 0 for record in invocation_records)
    assert all(record["usage"]["output_tokens"] > 0 for record in invocation_records)
    assert {record["upstream_session_id"] for record in invocation_records} == {
        invocation_records[0]["upstream_session_id"]
    }
    assert {record["codex_thread_id"] for record in invocation_records} == {
        "codex-thread-1"
    }


@pytest.mark.skipif(
    os.name != "posix" or not shutil.which("bwrap"),
    reason="the sandboxed compatibility command requires Linux bubblewrap",
)
def test_pinned_gepa_meta_harness_reads_skill_and_submits_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("gepa")
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    fake_codex = tmp_path / "home" / ".local" / "bin" / "codex"
    fake_codex.parent.mkdir(parents=True)
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
iteration = int(prompt.split("Run iteration ", 1)[1].split(" ", 1)[0])
candidates = []
for number in range(1, 4):
    candidate = Path(f"agents/iter{iteration}_red_{number}.txt")
    candidate.write_text("Return RED.", encoding="utf-8")
    candidates.append({
        "name": f"red-{iteration}-{number}",
        "file": str(candidate),
        "hypothesis": "The candidate remains intentionally non-perfect.",
        "axis": "exploration",
        "base": "baseline",
        "components": ["required-token"],
    })
pending = Path(f"state/pending_eval_iter{iteration}.json")
pending.write_text(json.dumps({
    "iteration": iteration,
    "candidates": candidates,
}), encoding="utf-8")
with (Path(os.environ["CODEX_ADAPTER_STATE_DIR"]) / "codex-invocations.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
print(json.dumps({"type": "thread.started", "thread_id": f"codex-thread-mh-{iteration}"}))
print(json.dumps({
    "type": "item.completed",
    "item": {"type": "agent_message", "text": "CANDIDATES: red"},
}))
print(json.dumps({
    "type": "turn.completed",
    "usage": {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 100},
}))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    state_dir = _sandboxed_runtime(tmp_path, monkeypatch, fake_codex)
    invocation_log = state_dir / "codex-invocations.jsonl"

    def evaluate(candidate: str) -> tuple[float, dict]:
        return 0.0, {
            "feedback": "Candidate is intentionally non-perfect.",
            "passed": False,
        }

    result = optimize_anything(
        seed_candidate="Return RED.",
        evaluator=evaluate,
        objective="Write a candidate containing BLUE.",
        config=OptimizeAnythingConfig(
            engine="meta_harness",
            max_evals=10,
            stop_at_score=None,
            sandbox=True,
            run_dir=str(tmp_path / "run"),
            output_dir=str(tmp_path / "output"),
            engine_config={
                "max_iterations": 3,
                "max_candidates_per_iter": 3,
            },
        ),
    )

    invocations = [
        json.loads(line)
        for line in invocation_log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(invocations) == 3
    assert all("resume" not in invocation for invocation in invocations)
    assert all(
        "Produce up to 3 candidate(s)." in invocation[-1]
        for invocation in invocations
    )
    assert all("--max-budget-usd" not in invocation for invocation in invocations)
    assert result.total_evals == 10
    assert result.metadata["meta_harness"]["iterations_run"] == 3
    assert result.metadata["meta_harness"]["stop_reason"] == "completed"
    assert len(list((state_dir / "invocation-slots").iterdir())) == 3

    invocation_records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (state_dir / "invocations").glob("*.json")
    ]
    assert len(invocation_records) == 3
    assert {record["terminal_status"] for record in invocation_records} == {"completed"}
    assert all(record["usage"]["input_tokens"] > 0 for record in invocation_records)
    assert all(record["usage"]["output_tokens"] > 0 for record in invocation_records)

    session_records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (state_dir / "sessions").glob("*.json")
    ]
    assert len(session_records) == 3
    assert len({record["upstream_session_id"] for record in session_records}) == 3
    assert {record["thread_id"] for record in session_records} == {
        "codex-thread-mh-1",
        "codex-thread-mh-2",
        "codex-thread-mh-3",
    }
