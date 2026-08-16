"""Pinned SDK client that replaces, rather than overlays, the child environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

import openai_codex.client as sdk_client
from openai_codex._initialize_metadata import validate_initialize_metadata
from openai_codex.client import CodexClient, CodexConfig


_ALLOWED_ENVIRONMENT = frozenset(
    {
        "APPDATA",
        "CODEX_HOME",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TZ",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)


def sanitized_environment(source: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in source.items()
        if key.upper() in _ALLOWED_ENVIRONMENT
    }


class SanitizedCodexClient(CodexClient):
    """CodexClient.start with a replacement environment for the child only."""

    def start(self) -> None:
        if self._proc is not None:
            return
        path_dirs: tuple[Path, ...] = ()
        if self.config.launch_args_override is not None:
            args = list(self.config.launch_args_override)
        else:
            codex_bin = sdk_client._resolve_codex_bin(self.config)
            if self.config.codex_bin is None:
                path_dirs = sdk_client._installed_codex_path_dirs()
            args = [str(codex_bin)]
            for override in self.config.config_overrides:
                args.extend(("--config", override))
            args.extend(("app-server", "--listen", "stdio://"))

        source = dict(os.environ)
        if self.config.env:
            source.update(self.config.env)
        environment = sanitized_environment(source)
        sdk_client._prepend_path_dirs(environment, path_dirs)
        self._proc = sdk_client.subprocess.Popen(
            args,
            stdin=sdk_client.subprocess.PIPE,
            stdout=sdk_client.subprocess.PIPE,
            stderr=sdk_client.subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.config.cwd,
            env=environment,
            bufsize=1,
        )
        self._start_stderr_drain_thread()
        self._start_reader_thread()

    def force_close(self) -> None:
        """Best-effort transport containment, including a half-started client."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            with suppress(Exception):
                proc.stdin.close()
        with suppress(Exception):
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            with suppress(Exception):
                proc.kill()
            with suppress(Exception):
                proc.wait(timeout=2)


def create_sanitized_codex(
    sdk: Any,
    config: CodexConfig,
    *,
    client_observer: Callable[[SanitizedCodexClient], None] | None = None,
) -> Any:
    client = SanitizedCodexClient(config=config)
    if client_observer is not None:
        client_observer(client)
    try:
        client.start()
        metadata = validate_initialize_metadata(client.initialize())
    except Exception:
        client.force_close()
        raise
    codex = sdk.Codex.__new__(sdk.Codex)
    codex._client = client
    codex._init = metadata
    return codex
