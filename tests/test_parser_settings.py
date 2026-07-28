from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents" / "skills" / "gepa-optimize-anything-codex" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from codex_claude_adapter import parse_agent_request


def test_claude_settings_json_is_not_treated_as_prompt(tmp_path):
    request = parse_agent_request(
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

    assert request.prompt == "actual prompt"
