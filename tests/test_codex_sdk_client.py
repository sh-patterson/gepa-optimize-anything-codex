from __future__ import annotations

import sys
from pathlib import Path


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

from codex_sdk_client import sanitized_environment  # noqa: E402


def test_sdk_client_removes_credentials_but_preserves_runtime_settings() -> None:
    sanitized = sanitized_environment(
        {
            "PATH": "runtime-path",
            "CODEX_HOME": "codex-home",
            "GH_TOKEN": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "PRIVATE_CLIENT_SECRET": "secret",
            "MY_SECRET": "secret",
            "PRIVATE_KEY": "secret",
            "DATABASE_URL": "postgres://user:pass@host/db",
            "OPENAI_BASE_URL": "https://redirect.invalid",
        }
    )

    assert sanitized == {"PATH": "runtime-path", "CODEX_HOME": "codex-home"}
