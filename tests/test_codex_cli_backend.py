from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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
SPEC = importlib.util.spec_from_file_location(
    "codex_cli_backend_test", SCRIPTS / "codex_cli_backend.py"
)
assert SPEC is not None and SPEC.loader is not None
cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cli
SPEC.loader.exec_module(cli)
from codex_runtime import InvocationSpec  # noqa: E402


def _spec(tmp_path: Path, **overrides: object) -> InvocationSpec:
    values = {
        "invocation_id": "cli-1",
        "prompt": "Return BLUE",
        "cwd": tmp_path.resolve(),
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "timeout_seconds": 1.0,
        "sandbox": "workspace-write",
    }
    values.update(overrides)
    return InvocationSpec(**values)


def test_command_is_codex_native_and_disables_network(tmp_path: Path) -> None:
    command = cli.build_command(
        _spec(tmp_path), Path("C:/codex.exe"), tmp_path / "schema.json"
    )
    joined = " ".join(str(part) for part in command)

    assert command[1:3] == ["exec", "--json"]
    assert "claude" not in joined.casefold()
    assert "sandbox_workspace_write.network_access=false" in command
    assert 'web_search="disabled"' in command
    assert command[command.index("--model") + 1] == "gpt-5.6-luna"
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--output-schema") + 1].endswith("schema.json")


def test_resume_command_preserves_thread_identity(tmp_path: Path) -> None:
    command = cli.build_command(
        _spec(tmp_path, resume_thread_id="thread-1"), Path("C:/codex.exe"), None
    )

    assert command[1:3] == ["exec", "resume"]
    assert command[-2:] == ["thread-1", "Return BLUE"]


def test_parser_requires_one_completed_terminal_with_usage() -> None:
    raw = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "thread-1"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "BLUE"},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 2,
                    "cached_input_tokens": 1,
                    "output_tokens": 3,
                },
            },
        )
    )

    terminal = cli.parse_jsonl(raw)

    assert terminal.status == "completed"
    assert terminal.thread_id == "thread-1"
    assert terminal.text == "BLUE"
    assert terminal.usage.total_tokens == 5


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps({"type": "turn.completed", "usage": {}}),
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                ),
                json.dumps({"type": "turn.failed", "error": "late"}),
            ]
        ),
    ],
)
def test_parser_fails_closed_on_ambiguous_output(raw: str) -> None:
    assert cli.parse_jsonl(raw).status == "invalid"


def test_probe_rejects_zero_exit_not_logged_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"")
    responses = iter(
        (
            subprocess.CompletedProcess([], 0, "codex-cli 1.0", ""),
            subprocess.CompletedProcess([], 0, "Not logged in", ""),
        )
    )
    monkeypatch.setattr(cli.subprocess, "run", lambda *_args, **_kwargs: next(responses))
    backend = cli.CodexCliBackend(executable=executable, environment={})

    probe = backend.probe()

    assert probe.available is False
    assert "authentication" in probe.reason


def test_cli_backend_records_completed_result_and_removes_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"")
    backend = cli.CodexCliBackend(executable=executable, environment={})
    backend._probe = cli.BackendProbe("codex_cli", True, "1.0", "chatgpt")
    seen: list[str] = []

    def fake_run(command: list[str], _spec: InvocationSpec) -> object:
        seen.extend(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "BLUE"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 2, "output_tokens": 3},
                        }
                    ),
                )
            ),
            "",
        )

    monkeypatch.setattr(backend, "_run", fake_run)
    schema = {
        "type": "object",
        "properties": {"candidate": {"type": "string"}},
        "required": ["candidate"],
    }

    result = backend.invoke(_spec(tmp_path, output_schema=schema))

    assert result.status == "completed"
    assert result.thread_id == "thread-1"
    schema_path = Path(seen[seen.index("--output-schema") + 1])
    assert schema_path.exists() is False


def test_child_environment_never_copies_api_keys(tmp_path: Path) -> None:
    environment = cli._child_environment(
        tmp_path,
        {
            "SAFE": "yes",
            "OPENAI_API_KEY": "secret",
            "CODEX_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "secret",
        },
    )

    assert environment == {"SAFE": "yes", "CODEX_HOME": str(tmp_path)}
