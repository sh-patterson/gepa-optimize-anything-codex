#!/usr/bin/env python3
"""Pre-flight checks for an optimize_anything run (gepa package) — fail fast before a long job.

    python preflight.py                         # checks gepa + reflection-LM creds
    python preflight.py --engine autoresearch --no-sandbox  # checks Codex + jq
    GEPA_REFLECTION_LM=anthropic/claude-sonnet-4-6 python preflight.py --test-lm

Exit code 0 = all good; non-zero = at least one blocker.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OK, BAD = "\033[32mOK\033[0m", "\033[31mFAIL\033[0m"
problems: list[str] = []

# The gepa backend's reflection LM defaults to openai/gpt-5.1; best_of_n's
# sampling model defaults to claude-sonnet-4-6 (see references/api.md).
DEFAULT_LM_BY_ENGINE = {"gepa": "openai/gpt-5.1", "best_of_n": "claude-sonnet-4-6"}
EXPECTED_LAUNCHER = Path(__file__).with_name("claude").resolve()
REQUIRED_CODEX_EXEC_FLAGS = {
    "--ignore-user-config",
    "--json",
    "--model",
    "--sandbox",
    "--skip-git-repo-check",
}


def check(label: str, ok: bool, fix: str = "") -> None:
    print(f"  [{OK if ok else BAD}] {label}")
    if not ok:
        problems.append(f"{label} — {fix}" if fix else label)


def _creds_for(lm: str) -> tuple[bool, str]:
    """Best-effort provider-credential check for a LiteLLM model id."""
    has_aws = bool(
        os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        or os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("AWS_PROFILE")
    )
    if "bedrock" in lm:
        return (
            has_aws,
            "export AWS creds (AWS_BEARER_TOKEN_BEDROCK / AWS_ACCESS_KEY_ID / AWS_PROFILE)",
        )
    if lm.startswith(("openai/", "gpt-")) or "gpt-5" in lm:
        return bool(os.environ.get("OPENAI_API_KEY")), "export OPENAI_API_KEY"
    if "claude" in lm or lm.startswith("anthropic/"):
        return bool(
            os.environ.get("ANTHROPIC_API_KEY")
        ) or has_aws, "export ANTHROPIC_API_KEY (or AWS creds)"
    # Unknown provider: accept any common key being present.
    any_key = bool(
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or has_aws
    )
    return any_key, "export your LiteLLM provider's API key"


def _codex_auth_available(codex: str) -> tuple[bool, str]:
    if os.environ.get("CODEX_API_KEY"):
        return True, "CODEX_API_KEY"
    if os.environ.get("OPENAI_API_KEY"):
        return True, "OPENAI_API_KEY"
    try:
        proc = subprocess.run(
            [codex, "login", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return False, ""
    return (
        proc.returncode == 0,
        "Codex CLI login configuration (token freshness untested)"
        if proc.returncode == 0
        else "",
    )


def _is_bundled_launcher(command: str) -> bool:
    try:
        return Path(command).resolve() == EXPECTED_LAUNCHER
    except OSError:
        return False


def _state_dir_writable(raw_path: str | None) -> bool:
    if not raw_path:
        return False
    state_dir = Path(raw_path).expanduser()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=state_dir):
            pass
    except OSError:
        return False
    return True


def _codex_exec_surface(codex: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [codex, "exec", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError as exc:
        return False, str(exc)
    output = f"{proc.stdout}\n{proc.stderr}"
    missing = sorted(flag for flag in REQUIRED_CODEX_EXEC_FLAGS if flag not in output)
    if proc.returncode != 0:
        return False, f"`codex exec --help` exited {proc.returncode}"
    if missing:
        return False, f"missing flags: {', '.join(missing)}"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--engine",
        default="gepa",
        choices=["gepa", "best_of_n", "autoresearch", "meta_harness"],
    )
    ap.add_argument(
        "--no-sandbox",
        action="store_true",
        help="confirm that the run sets sandbox=False, required by this adapter",
    )
    ap.add_argument(
        "--test-lm",
        action="store_true",
        help="make a 1-call round-trip to the reflection LM (costs a few tokens)",
    )
    a = ap.parse_args()

    print("== optimize_anything preflight ==")

    # 1) import + the correct API surface
    try:
        import gepa  # noqa
        from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything  # noqa

        check(
            f"import gepa ({getattr(gepa, '__version__', '?')}) + optimize_anything",
            True,
        )
    except Exception as e:  # noqa
        check(
            "import gepa + optimize_anything",
            False,
            "pip install -e '.[live]' from this adapter repository",
        )
        print(f"      {e}")
        return _report()

    # 2) LM credentials (in-process engines that call an LLM directly)
    lm = os.environ.get("GEPA_REFLECTION_LM", "")
    if a.engine in ("gepa", "best_of_n"):
        effective_lm = lm or DEFAULT_LM_BY_ENGINE[a.engine]
        if not lm:
            print(f"      GEPA_REFLECTION_LM unset -> engine default '{effective_lm}'")
        ok, fix = _creds_for(effective_lm)
        check(f"LLM creds present for '{effective_lm}'", ok, fix)

    # 3) agentic engines need the Codex-backed compatibility command.
    if a.engine in ("autoresearch", "meta_harness"):
        cli = shutil.which("claude")
        codex = shutil.which("codex")
        check(
            "Linux host (the Codex compatibility command is Linux-only)",
            sys.platform.startswith("linux"),
            "run the agentic engines from Linux",
        )
        check(
            f"Bundled Codex adapter `claude` on PATH (required by {a.engine})",
            bool(cli) and _is_bundled_launcher(cli),
            "prepend this skill's scripts directory to PATH",
        )
        check("`codex` CLI on PATH", bool(codex), "install and authenticate Codex CLI")
        codex_surface_ok, codex_surface_problem = (
            _codex_exec_surface(codex) if codex else (False, "")
        )
        check(
            "Codex CLI exposes the required `exec` flags",
            codex_surface_ok,
            codex_surface_problem or "install a supported Codex CLI",
        )
        codex_auth_ok, codex_auth_source = (
            _codex_auth_available(codex) if codex else (False, "")
        )
        check(
            "Codex auth configuration (API key or existing CLI login)",
            codex_auth_ok,
            "set CODEX_API_KEY/OPENAI_API_KEY or run `codex login`",
        )
        if codex_auth_source:
            print(f"      authenticated through {codex_auth_source}")
        state_dir = os.environ.get("CODEX_ADAPTER_STATE_DIR")
        check(
            "CODEX_ADAPTER_STATE_DIR is set and writable",
            _state_dir_writable(state_dir),
            "export CODEX_ADAPTER_STATE_DIR to a writable private directory",
        )
        if cli:
            print(f"      claude adapter -> {cli}")
        if codex:
            print(f"      codex -> {codex}")
        if a.engine == "autoresearch":
            check(
                "`jq` on PATH (used by the generated eval.sh)",
                bool(shutil.which("jq")),
                "install jq",
            )
        if not a.no_sandbox:
            check(
                "sandbox=False is required for the Codex compatibility command",
                False,
                "pass --no-sandbox and set sandbox=False in OptimizeAnythingConfig",
            )

    # 4) optional live LM round-trip
    if a.test_lm and a.engine in ("gepa", "best_of_n"):
        target = lm or DEFAULT_LM_BY_ENGINE[a.engine]
        try:
            from gepa.lm import LM

            out = LM(target)("Reply with the single word: ok")
            check(
                f"LM 1-call round-trip ({target})",
                bool(out),
                "LM returned empty; check model id / creds / region",
            )
        except Exception as e:  # noqa
            check(f"LM 1-call round-trip ({target})", False, str(e)[:160])

    return _report()


def _report() -> int:
    print()
    if problems:
        print(f"\033[31m{len(problems)} blocker(s):\033[0m")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\033[32mAll no-call configuration checks passed.\033[0m")
    print("Authentication freshness requires a live smoke.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
