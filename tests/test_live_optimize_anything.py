from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
RELEASE_DOGFOOD = ROOT / "scripts" / "release_dogfood.py"


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_CODEX_AGENT_SMOKE") != "1",
    reason="requires paid GEPA and Codex calls",
)
@pytest.mark.parametrize("engine", ("autoresearch", "meta_harness"))
def test_release_dogfood_improves_a_deterministic_text_candidate(engine: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(RELEASE_DOGFOOD), "--engine", engine],
        cwd=ROOT,
        env={**os.environ, "RUN_CODEX_AGENT_SMOKE": "1"},
        capture_output=True,
        text=True,
        timeout=700,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["status"] == "success"
    assert receipt["engine"] == engine
    assert receipt["policy"]["target_model"] == "gpt-5.6-luna"
    assert receipt["policy"]["reasoning_effort"] == "high"
    assert receipt["policy"]["sandbox"] is True
    assert receipt["source"]["sandbox_runtime"] == "bubblewrap"
    assert receipt["result"]["seed_candidate"] == "Return RED."
    assert "BLUE" in receipt["result"]["best_candidate"]
    assert receipt["result"]["best_score"] == 1.0
    assert receipt["result"]["total_evals"] >= 2
    assert sum(receipt["usage"].values()) > 0
    assert receipt["cost"]["estimated_usd"] > 0
    assert receipt["cost"]["adapter_reported_usd"] > 0
    assert receipt["cost"]["agreement"]
    assert receipt["session_mapping"]
    assert all(
        mapping["upstream_session_id"] and mapping["codex_thread_id"]
        for mapping in receipt["session_mapping"]
    )

    receipt_path = Path(receipt["receipt_path"])
    assert receipt_path.is_file()
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt
    assert receipt["file_hashes"]
    for raw_path, expected_hash in receipt["file_hashes"].items():
        artifact_path = Path(raw_path)
        assert artifact_path != receipt_path
        assert artifact_path.is_file()
        assert expected_hash == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
