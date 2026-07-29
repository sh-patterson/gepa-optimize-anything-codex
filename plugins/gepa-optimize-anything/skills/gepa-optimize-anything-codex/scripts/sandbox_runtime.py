#!/usr/bin/env python3
"""Stage and verify the Codex compatibility runtime inside GEPA's bwrap jail."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
LAUNCHER_SOURCE = SCRIPT_DIR / "claude"
ADAPTER_SOURCE = SCRIPT_DIR / "codex_claude_adapter.py"
RUNTIME_NAME = "gepa-optimize-anything-codex"
NPM_INSTALL_FIX = 'npm install --prefix "$HOME/.local" @openai/codex@0.146.0'
NPM_PATH_FIX = 'export PATH="$HOME/.local/node_modules/.bin:$PATH"'
SANDBOX_PYTHON = Path("/usr/bin/python3")


@dataclass(frozen=True)
class RuntimePaths:
    """All paths the sandboxed compatibility runtime is allowed to own."""

    home: Path
    stage_bin: Path
    launcher: Path
    adapter_module: Path
    codex: Path
    codex_home: Path
    state_dir: Path


def _home(env: dict[str, str] | None = None) -> Path:
    raw_home = (env or os.environ).get("HOME")
    return Path(raw_home).expanduser().resolve() if raw_home else Path.home().resolve()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _allowed_codex_roots(home: Path) -> tuple[Path, ...]:
    return (home / ".local", Path("/usr"), Path("/usr/local"))


def resolve_codex(home: Path, env: dict[str, str] | None = None) -> Path:
    environment = env or os.environ
    configured = environment.get("CODEX_CLI")
    candidate = configured or shutil.which("codex", path=environment.get("PATH"))
    if not candidate:
        raise RuntimeError(
            f"Codex CLI was not found. Run `{NPM_INSTALL_FIX}`, then `{NPM_PATH_FIX}`."
        )
    codex = Path(candidate).expanduser().resolve()
    if not codex.is_file() or not os.access(codex, os.X_OK):
        raise RuntimeError(f"Codex CLI is not executable: {codex}")
    if not any(_within(codex, root) for root in _allowed_codex_roots(home)):
        roots = ", ".join(str(root) for root in _allowed_codex_roots(home))
        raise RuntimeError(
            f"Codex CLI must resolve under {roots}; run `{NPM_INSTALL_FIX}`, "
            f"then `{NPM_PATH_FIX}`."
        )
    return codex


def runtime_paths(
    *,
    home: Path | None = None,
    state_dir: Path | None = None,
    env: dict[str, str] | None = None,
) -> RuntimePaths:
    environment = env or os.environ
    resolved_home = (home or _home(environment)).expanduser().resolve()
    cache_root = resolved_home / ".cache" / RUNTIME_NAME
    configured_state = state_dir or (
        Path(environment["CODEX_ADAPTER_STATE_DIR"])
        if environment.get("CODEX_ADAPTER_STATE_DIR")
        else cache_root / "runs" / uuid.uuid4().hex
    )
    resolved_state = configured_state.expanduser().resolve()
    if not _within(resolved_state, cache_root):
        raise RuntimeError(
            "CODEX_ADAPTER_STATE_DIR must be an explicit directory under "
            f"{cache_root}."
        )
    stage_bin = resolved_home / ".local" / "share" / RUNTIME_NAME / "bin"
    return RuntimePaths(
        home=resolved_home,
        stage_bin=stage_bin,
        launcher=stage_bin / "claude",
        adapter_module=stage_bin / "codex_claude_adapter.py",
        codex=resolve_codex(resolved_home, environment),
        codex_home=cache_root / "codex",
        state_dir=resolved_state,
    )


def _copy_if_changed(source: Path, destination: Path, executable: bool = False) -> None:
    source_bytes = source.read_bytes()
    if not destination.exists() or destination.read_bytes() != source_bytes:
        shutil.copyfile(source, destination)
    if executable:
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR)


def stage_runtime(paths: RuntimePaths) -> RuntimePaths:
    paths.stage_bin.mkdir(parents=True, exist_ok=True)
    paths.codex_home.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    (paths.home / ".claude").mkdir(parents=True, exist_ok=True)
    claude_json = paths.home / ".claude.json"
    if not claude_json.exists():
        claude_json.write_text("{}\n", encoding="utf-8")
    _copy_if_changed(LAUNCHER_SOURCE, paths.launcher, executable=True)
    _copy_if_changed(ADAPTER_SOURCE, paths.adapter_module)
    return paths


def runtime_environment(
    paths: RuntimePaths, env: dict[str, str] | None = None
) -> dict[str, str]:
    environment = dict(os.environ if env is None else env)
    current_path = environment.get("PATH", "")
    environment.update(
        {
            "HOME": str(paths.home),
            "PATH": f"{paths.stage_bin}{os.pathsep}{current_path}",
            "CODEX_CLI": str(paths.codex),
            "CODEX_HOME": str(paths.codex_home),
            "CODEX_ADAPTER_STATE_DIR": str(paths.state_dir),
        }
    )
    return environment


def login_runtime(paths: RuntimePaths) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(paths.codex), "login"],
        cwd=paths.home,
        env=runtime_environment(paths),
        check=False,
    )


def _probe_script() -> str:
    return """
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

launcher = Path(os.environ["GEPA_CODEX_LAUNCHER"])
adapter = Path(os.environ["GEPA_CODEX_ADAPTER_MODULE"])
assert launcher.is_file() and os.access(launcher, os.X_OK)
assert Path(shutil.which("claude")).resolve() == launcher.resolve()
spec = importlib.util.spec_from_file_location("codex_claude_adapter", adapter)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)
help_result = subprocess.run(
    [os.environ["CODEX_CLI"], "exec", "--help"],
    capture_output=True,
    text=True,
    check=False,
)
assert help_result.returncode == 0, help_result.stderr
if not os.environ.get("CODEX_API_KEY"):
    login_result = subprocess.run(
        [os.environ["CODEX_CLI"], "login", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert login_result.returncode == 0, login_result.stderr
for name in ("CODEX_HOME", "CODEX_ADAPTER_STATE_DIR"):
    directory = Path(os.environ[name])
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / ".sandbox-runtime-probe"
    marker.write_text("ok", encoding="utf-8")
    assert marker.read_text(encoding="utf-8") == "ok"
    marker.unlink()
"""


def probe_runtime(paths: RuntimePaths) -> subprocess.CompletedProcess[str]:
    from gepa.oa.sandbox import bwrap_prefix

    environment = runtime_environment(paths)
    environment["GEPA_CODEX_LAUNCHER"] = str(paths.launcher)
    environment["GEPA_CODEX_ADAPTER_MODULE"] = str(paths.adapter_module)
    with tempfile.TemporaryDirectory(prefix="gepa-codex-probe-") as raw_work_dir:
        work_dir = Path(raw_work_dir)
        return subprocess.run(
            [*bwrap_prefix(work_dir), str(SANDBOX_PYTHON), "-c", _probe_script()],
            cwd=work_dir,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", nargs="?", choices=["stage", "login", "probe"], default="stage"
    )
    args = parser.parse_args(argv)
    try:
        paths = stage_runtime(runtime_paths())
        if args.command == "login":
            return login_runtime(paths).returncode
        if args.command == "probe":
            result = probe_runtime(paths)
            if result.returncode != 0:
                print(result.stderr or result.stdout, file=sys.stderr, end="")
                return result.returncode or 1
        print(paths.stage_bin)
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"sandbox runtime error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
