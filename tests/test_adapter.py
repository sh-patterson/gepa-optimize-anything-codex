from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    ROOT
    / "plugins"
    / "gepa-optimize-anything"
    / "skills"
    / "gepa-optimize-anything-codex"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from codex_claude_adapter import (  # noqa: E402
    _codex_command,
    _load_session_thread,
    _save_session_thread,
    invoke_codex,
    estimated_luna_cost,
    parse_agent_request,
    parse_codex_output,
    scrubbed_env,
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
        resumed, "/bin/codex", "codex-thread-1"
    )

    assert initial_command[:2] == ["/bin/codex", "exec"]
    assert resumed_command[:3] == ["/bin/codex", "exec", "resume"]
    assert "codex-thread-1" in resumed_command
    assert "--ephemeral" not in initial_command


def test_upstream_model_is_translated_to_a_distinct_codex_model(tmp_path):
    request = parse_agent_request(
        [
            "--print",
            "--session-id",
            "upstream-1",
            "--model",
            "claude-sonnet-4-6",
            "prompt",
        ],
        tmp_path,
    )

    command = _codex_command(request, "/bin/codex", None)

    assert request.source_model == "claude-sonnet-4-6"
    assert request.target_model == "gpt-5.6-luna"
    assert command[command.index("-m") + 1] == "gpt-5.6-luna"
    assert "claude-sonnet-4-6" not in command
    assert "features.standalone_web_search=false" in command


def test_unknown_source_model_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unsupported source model"):
        parse_agent_request(
            [
                "--print",
                "--session-id",
                "upstream-1",
                "--model",
                "unknown-model",
                "prompt",
            ],
            tmp_path,
        )


def test_known_disallowed_tools_assignment_is_accepted(tmp_path):
    request = parse_agent_request(
        [
            "--print",
            "--disallowedTools=WebFetch,WebSearch",
            "--session-id",
            "upstream-1",
            "prompt",
        ],
        tmp_path,
    )

    assert request.prompt == "prompt"


@pytest.mark.parametrize(
    "argv",
    [
        [
            "--print",
            "--disallowedTools=Write,Edit",
            "--session-id",
            "upstream-1",
            "prompt",
        ],
        [
            "--print",
            "--disallowedTools",
            "WebFetch,WebSearch",
            "--session-id",
            "upstream-1",
            "prompt",
        ],
        [
            "--print",
            "--permission-mode",
            "default",
            "--session-id",
            "upstream-1",
            "prompt",
        ],
        [
            "--print",
            "--output-format",
            "text",
            "--session-id",
            "upstream-1",
            "prompt",
        ],
        [
            "--print",
            "--allowedTools",
            "Read",
            "--session-id",
            "upstream-1",
            "prompt",
        ],
    ],
)
def test_unimplemented_policy_or_output_flags_are_rejected(tmp_path, argv):
    with pytest.raises(ValueError):
        parse_agent_request(argv, tmp_path)


def test_session_id_and_resume_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        parse_agent_request(
            [
                "--print",
                "--session-id",
                "upstream-1",
                "--resume",
                "upstream-1",
                "prompt",
            ],
            tmp_path,
        )


def test_claude_settings_are_rejected_as_untranslatable(tmp_path):
    with pytest.raises(ValueError, match="--settings"):
        parse_agent_request(
            [
                "--print",
                "--session-id",
                "upstream-1",
                "--settings",
                '{"sandbox":{"enabled":true}}',
                "actual prompt",
            ],
            tmp_path,
        )


def test_unknown_flag_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unsupported Claude CLI flag"):
        parse_agent_request(
            ["--print", "--unknown-flag", "--session-id", "upstream-1", "prompt"],
            tmp_path,
        )


def test_codex_json_events_become_a_completed_terminal_state():
    raw = '{"type":"thread.started","thread_id":"codex-thread-1"}\n{"type":"item.completed","item":{"type":"agent_message","text":"finished"}}\n{"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":100,"output_tokens":100}}'

    terminal = parse_codex_output(raw)

    assert terminal.thread_id == "codex-thread-1"
    assert terminal.final_message == "finished"
    assert terminal.status == "completed"
    assert terminal.usage == {
        "input_tokens": 1000,
        "cached_input_tokens": 100,
        "output_tokens": 100,
    }


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        ('{"type":"thread.started","thread_id":"codex-thread-1"}', "missing"),
        (
            '{"type":"thread.started","thread_id":"codex-thread-1"}\n'
            '{"type":"turn.failed","error":{"message":"quota exceeded"}}',
            "failed",
        ),
        (
            '{"type":"thread.started","thread_id":"codex-thread-1"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1,'
            '"output_tokens":1}}\n'
            '{"type":"error","error":{"message":"late failure"}}',
            "failed",
        ),
        (
            '{"type":"thread.started","thread_id":"codex-thread-1"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":-1,'
            '"output_tokens":1}}',
            "failed",
        ),
    ],
)
def test_incomplete_or_failed_codex_jsonl_is_not_success(raw, status):
    terminal = parse_codex_output(raw)

    assert terminal.status == status


def test_malformed_codex_jsonl_fails_closed():
    terminal = parse_codex_output(
        '{"type":"thread.started","thread_id":"codex-thread-1"}\n'
        "not-json\n"
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
    )

    assert terminal.status == "failed"
    assert terminal.error == "Codex emitted malformed JSONL"


@pytest.mark.parametrize(
    ("raw", "expected_error"),
    [
        ("[]", "Codex emitted a non-object JSONL event"),
        (
            '{"type":"item.completed","item":[]}',
            "Codex emitted an invalid item event",
        ),
    ],
)
def test_wrong_shaped_codex_jsonl_fails_closed(raw, expected_error):
    terminal = parse_codex_output(raw)

    assert terminal.status == "failed"
    assert terminal.error == expected_error


def test_unrelated_tool_credentials_survive_environment_scrubbing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "remove")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "remove")
    monkeypatch.setenv("GH_TOKEN", "keep")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "keep")

    child_env = scrubbed_env()

    assert "ANTHROPIC_API_KEY" not in child_env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in child_env
    assert child_env["GH_TOKEN"] == "keep"
    assert child_env["AWS_SESSION_TOKEN"] == "keep"


def test_cost_estimate_includes_cache_write_and_long_context_rates():
    ordinary = estimated_luna_cost(
        {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 100}
    )
    long_context = estimated_luna_cost(
        {"input_tokens": 273_000, "cached_input_tokens": 0, "output_tokens": 100}
    )

    assert ordinary == pytest.approx(0.001735)
    assert long_context == pytest.approx(0.6834)


def test_max_budget_is_rejected_before_codex_spawn(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    marker = tmp_path / "spawned"
    fake_codex.write_text(
        "#!/bin/sh\n"
        f"touch {marker}\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("CODEX_CLI", str(fake_codex))
    monkeypatch.setenv("CODEX_ADAPTER_STATE_DIR", str(tmp_path / "adapter-state"))
    request = parse_agent_request(
        [
            "--print",
            "--session-id",
            "upstream-1",
            "--max-budget-usd",
            "0.20",
            "prompt",
        ],
        tmp_path,
    )

    with pytest.raises(ValueError, match="--max-budget-usd"):
        invoke_codex(request)

    assert not marker.exists()


def test_zero_exit_without_turn_completed_fails_closed(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "echo '{\"type\":\"thread.started\",\"thread_id\":\"codex-thread-1\"}'\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("CODEX_CLI", str(fake_codex))
    monkeypatch.setenv("CODEX_ADAPTER_STATE_DIR", str(tmp_path / "adapter-state"))
    request = parse_agent_request(
        ["--print", "--session-id", "upstream-1", "prompt"], tmp_path
    )

    run = invoke_codex(request)

    assert run.returncode == 1
    assert "without a completed turn" in run.stderr


def test_completed_turn_without_an_agent_message_fails_closed(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "echo '{\"type\":\"thread.started\",\"thread_id\":\"codex-thread-1\"}'\n"
        "echo '{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}'\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("CODEX_CLI", str(fake_codex))
    monkeypatch.setenv("CODEX_ADAPTER_STATE_DIR", str(tmp_path / "adapter-state"))
    request = parse_agent_request(
        ["--print", "--session-id", "upstream-1", "prompt"], tmp_path
    )

    run = invoke_codex(request)

    assert run.returncode == 1
    assert "without a final agent message" in run.stderr
    assert run.usage == {"input_tokens": 1, "output_tokens": 1}


def test_error_receipt_preserves_observed_usage_and_cost(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "echo '{\"type\":\"thread.started\",\"thread_id\":\"codex-thread-1\"}'\n"
        "echo '{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":1000,"
        "\"cached_input_tokens\":100,\"output_tokens\":100}}'\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("CODEX_CLI", str(fake_codex))
    monkeypatch.setenv("CODEX_ADAPTER_STATE_DIR", str(tmp_path / "adapter-state"))
    launcher = SCRIPTS / "claude"

    proc = subprocess.run(
        [
            str(launcher),
            "--print",
            "prompt",
            "--session-id",
            "upstream-1",
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
            "--disallowedTools=WebFetch,WebSearch",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(proc.stdout)

    assert proc.returncode == 1
    assert payload["is_error"]
    assert payload["usage"]["input_tokens"] == 1000
    assert payload["total_cost_usd"] == pytest.approx(0.001735)
    assert payload["adapter_cost_status"].startswith("standard_tier")


def test_atomic_per_session_records_do_not_share_concurrent_writes(tmp_path):
    state_dir = tmp_path / "adapter-state"
    barrier = threading.Barrier(2)

    def save(session_id: str, thread_id: str) -> None:
        barrier.wait()
        _save_session_thread(state_dir, session_id, thread_id)

    first = threading.Thread(target=save, args=("upstream-1", "codex-thread-1"))
    second = threading.Thread(target=save, args=("upstream-2", "codex-thread-2"))
    first.start()
    second.start()
    first.join()
    second.join()

    assert _load_session_thread(state_dir, "upstream-1") == "codex-thread-1"
    assert _load_session_thread(state_dir, "upstream-2") == "codex-thread-2"
    assert len(list((state_dir / "sessions").glob("*.json"))) == 2


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_adapter_termination_terminates_the_codex_process_group(tmp_path):
    fake_codex = tmp_path / "codex"
    child_pid = tmp_path / "child.pid"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "sleep 60 &\n"
        f"echo $! > {child_pid}\n"
        "wait\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    launcher = SCRIPTS / "claude"
    env = {
        **os.environ,
        "CODEX_CLI": str(fake_codex),
        "CODEX_ADAPTER_STATE_DIR": str(tmp_path / "adapter-state"),
    }
    proc = subprocess.Popen(
        [str(launcher), "--print", "prompt", "--session-id", "upstream-1"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not child_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child_pid.exists()
    child = int(child_pid.read_text(encoding="utf-8"))

    started = time.monotonic()
    os.kill(proc.pid, signal.SIGTERM)
    proc.communicate(timeout=10)
    assert time.monotonic() - started < 5

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{child}/stat").read_text(encoding="utf-8").split()[2]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("Codex child process remained alive after adapter termination")


def test_claude_command_invokes_fake_codex_and_returns_json(tmp_path):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
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
    assert payload["total_cost_usd"] == pytest.approx(0.001735)
    assert (
        payload["adapter_cost_status"]
        == "standard_tier_upper_estimate_from_observed_usage"
    )


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
    assert proc.returncode == 0, proc.stderr
    assert payload["subtype"] == "success"
    assert payload["result"].strip().lower() == "ok"
    assert payload["total_cost_usd"] > 0
