"""No-model readiness probe for the Codex-native optimizer runtime."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from codex_runtime import CodexRuntime, create_runtime


def readiness_report(runtime: CodexRuntime) -> dict[str, object]:
    try:
        probe = runtime.prepare()
        return {
            "schema_version": 1,
            "ready": True,
            "selected_provider": probe.provider,
            "runtime_version": probe.runtime_version,
            "auth_mode": probe.auth_mode,
            "fallback_reason": runtime.fallback_reason,
            "provider_probe": asdict(probe),
            "provider_call_made": False,
        }
    except Exception as exc:
        return {
            "schema_version": 1,
            "ready": False,
            "selected_provider": None,
            "reason": f"{type(exc).__name__}: {exc}"[:500],
            "provider_call_made": False,
        }
    finally:
        runtime.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--no-cli-fallback", action="store_true")
    args = parser.parse_args(argv)
    runtime = create_runtime(
        evidence_dir=args.evidence_dir.resolve(),
        codex_home=args.codex_home.resolve() if args.codex_home else None,
        allow_cli_fallback=not args.no_cli_fallback,
    )
    report = readiness_report(runtime)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
