from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents" / "skills" / "gepa-optimize-anything-codex" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from codex_claude_adapter import (
    _codex_command,
    parse_agent_request,
    parse_codex_output,
)


def test_initial_and_resumed_commands_map_to_codex_threads(tmp_path):
    initial = parse_agent_request(
        [
            "--print",
            "--session-id",
            "upstream-1",
            "--model",
            "gpt-5.6-luna",
            "--effort",
            "medium",
            "first prompt",
        ],
        tmp_path,
    )
    resumed = parse_agent_request(
        [
            "--print",
            "--resume",
            "upstream-1",
            "--model",
            "gpt-5.6-luna",
            "second prompt",
        ],
        tmp_path,
    )

    initial_command = _codex_command(initial, "/bin/codex", {})
    resumed_command = _codex_command(
        resumed, "/bin/codex", {"upstream-1": "codex-thread-1"}
    )

    assert initial_command[:2] == ["/bin/codex", "exec"]
    assert resumed_command[:3] == ["/bin/codex", "exec", "resume"]
    assert "codex-thread-1" in resumed_command
    assert "--ephemeral" not in initial_command


def test_codex_json_events_become_claude_result_fields():
    raw = '{"type":"thread.started","thread_id":"codex-thread-1"}\n{"type":"item.completed","item":{"type":"agent_message","text":"finished"}}\n{"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":100,"output_tokens":100}}'

    assert parse_codex_output(raw) == (
        "codex-thread-1",
        "finished",
        {
            "input_tokens": 1000,
            "cached_input_tokens": 100,
            "output_tokens": 100,
        },
    )


def test_claude_command_invokes_fake_codex_and_returns_json(tmp_path):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        'test -d "$CODEX_HOME" || exit 23\n'
        'echo \'{"type":"thread.started","thread_id":"codex-thread-1"}\'\n'
        'echo \'{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"finished"}}\'\n'
        'echo \'{"type":"turn.completed","usage":{"input_tokens":1000,'
        '"cached_input_tokens":100,"output_tokens":100}}\'\n',
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    launcher = SCRIPTS / "claude"
    env = {
        **os.environ,
        "CODEX_CLI": str(fake_codex),
        "CODEX_ADAPTER_STATE_DIR": str(tmp_path / "adapter-state"),
        "OPENAI_API_KEY": "test-key",
    }

    proc = subprocess.run(
        [
            str(launcher),
            "--print",
            "do the work",
            "--session-id",
            "upstream-session-1",
            "--output-format",
            "json",
            "--model",
            "gpt-5.6-luna",
            "--effort",
            "medium",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    payload = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert payload["subtype"] == "success"
    assert payload["result"] == "finished"
    assert payload["total_cost_usd"] == pytest.approx(0.00151)


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_CODEX_AGENT_SMOKE") != "1",
    reason="requires one paid Codex API call",
)
def test_live_codex_round_trip(tmp_path):
    launcher = SCRIPTS / "claude"
    env = {
        **os.environ,
        "CODEX_ADAPTER_STATE_DIR": str(tmp_path / "adapter-state"),
    }
    proc = subprocess.run(
        [
            str(launcher),
            "--print",
            "Reply with the single word: ok",
            "--session-id",
            "live-smoke",
            "--output-format",
            "json",
            "--model",
            "gpt-5.6-luna",
            "--effort",
            "medium",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0, proc.stder
    assert payload["subtype"] == "success"
    assert payload["result"].strip().lower() == "ok"
    assert payload["total_cost_usd"] > 0
