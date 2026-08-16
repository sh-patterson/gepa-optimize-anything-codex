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

import codex_runtime as runtime  # noqa: E402
import native_preflight as preflight  # noqa: E402


class FakeRuntime:
    fallback_reason = None

    def __init__(self, available: bool) -> None:
        self.available = available
        self.closed = False

    def prepare(self) -> object:
        if not self.available:
            raise runtime.BackendUnavailable("no authenticated runtime")
        return runtime.BackendProbe(
            provider="app_server",
            available=True,
            runtime_version="0.144.4",
            auth_mode="chatgpt",
        )

    def close(self) -> None:
        self.closed = True


def test_preflight_reports_readiness_without_provider_call() -> None:
    native = FakeRuntime(True)
    report = preflight.readiness_report(native)

    assert report["ready"] is True
    assert report["selected_provider"] == "app_server"
    assert report["provider_call_made"] is False
    assert native.closed is True


def test_preflight_fails_closed_and_sanitizes_reason() -> None:
    native = FakeRuntime(False)
    report = preflight.readiness_report(native)

    assert report["ready"] is False
    assert report["selected_provider"] is None
    assert "no authenticated runtime" in report["reason"]
    assert report["provider_call_made"] is False
    assert native.closed is True
