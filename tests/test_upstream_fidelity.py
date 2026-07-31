from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]
PINNED_GEPA_COMMIT = "3a6f93c5dd0beb68825973b3b2f2cae23060bbbb"
PINNED_GEPA_DEPENDENCY = (
    "gepa[full] @ git+https://github.com/sh-patterson/gepa.git@"
    f"{PINNED_GEPA_COMMIT}"
)
SKILL = (
    ROOT
    / "plugins"
    / "gepa-optimize-anything"
    / "skills"
    / "gepa-optimize-anything-codex"
)

PINNED_UPSTREAM_SHA256 = {
    "SKILL.md": "d2addb90007d93da8bd93c556940a89b686ae0de51f9f4d3ce47041d23aaa599",
    "references/api.md": (
        "556f6b7968edcb50afd80c23560a8bad0155ee9633b0f828cb954da267d98f5d"
    ),
    "references/gotchas.md": (
        "f299dab0819095fdaf430a4715fcdd7c1f5518579470d4c6be76dc78f758b4d2"
    ),
    "references/tracking.md": "f74dae340715fe9cd5a4a99c0f6a62c97dff6f65cfdd8e6a35e26dbb75c0da2c",
    "references/writing_evaluators.md": (
        "2b746c0dabd152c75fb31dd6e7737b63cddc76365c5cc0b1e02d8935ff96fbba"
    ),
}

REVIEWED_LOCAL_SHA256 = {
    "SKILL.md": "04229804353e801614a20071f1006bdb5cc865addec04c289bd6deb61b46275f",
    "references/api.md": (
        "dbea32c545a2df516ff00c3baaa3541ec6b5830a76444cbb60d8b26810d3de8c"
    ),
    "references/gotchas.md": (
        "d28c21022177e63409052cd4c34e9c1da982af7b8524c9f4f88ba29e3f87245c"
    ),
    "references/tracking.md": PINNED_UPSTREAM_SHA256["references/tracking.md"],
    "references/writing_evaluators.md": PINNED_UPSTREAM_SHA256[
        "references/writing_evaluators.md"
    ],
}

KNOWN_TRUNCATIONS = {
    "selection, o\n",
    "evaluators fo\n",
    "higher=bette\n",
    "a provide\n",
    "result pe\n",
    "no longe\n",
    "run_di\n",
    "; o\n",
    "server fo\n",
    "keep it fo\n",
    "typo'd o\n",
    "and/o\n",
    "Whateve\n",
    "same orde\n",
    "inside you\n",
    "C-extension o\n",
    "higher = bette\n",
    "the adapte\n",
    "frontie\n",
    "want it fo\n",
}


def normalized_bytes(path: Path) -> bytes:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").encode()


def test_copied_docs_match_reviewed_upstream_adaptation() -> None:
    assert set(REVIEWED_LOCAL_SHA256) == set(PINNED_UPSTREAM_SHA256)
    adapted = {
        path
        for path in PINNED_UPSTREAM_SHA256
        if PINNED_UPSTREAM_SHA256[path] != REVIEWED_LOCAL_SHA256[path]
    }
    assert adapted == {"SKILL.md", "references/api.md", "references/gotchas.md"}
    for relative_path, expected_hash in REVIEWED_LOCAL_SHA256.items():
        actual_hash = hashlib.sha256(normalized_bytes(SKILL / relative_path)).hexdigest()
        assert actual_hash == expected_hash, relative_path


def test_gepa_pin_is_consistent_across_install_paths() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    live_dependencies = pyproject["project"]["optional-dependencies"]["live"]
    upstream = (ROOT / "UPSTREAM.md").read_text(encoding="utf-8")
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    assert live_dependencies == [PINNED_GEPA_DEPENDENCY]
    assert PINNED_GEPA_COMMIT in upstream
    assert PINNED_GEPA_DEPENDENCY in skill


def test_upstream_autoresearch_repair_gate_is_explicit() -> None:
    upstream = (ROOT / "UPSTREAM.md").read_text(encoding="utf-8")

    assert "EvalServer wait_for_idle()" in upstream
    assert "completed evaluation receipt" in upstream
    assert "evaluation N+1" in upstream
    assert "Do not update the pinned commit" in upstream


def test_upstream_copy_has_no_known_truncations() -> None:
    copied_text = "\n".join(
        path.read_text(encoding="utf-8") for path in SKILL.rglob("*.md")
    )
    found = sorted(fragment.strip() for fragment in KNOWN_TRUNCATIONS if fragment in copied_text)
    assert found == []


def test_adapter_invocation_journal_stays_metadata_only() -> None:
    adapter = (SKILL / "scripts" / "codex_claude_adapter.py").read_text(
        encoding="utf-8"
    )
    start = adapter.index("def _save_invocation_record")
    end = adapter.index("\ndef _codex_command", start)
    journal = adapter[start:end]

    assert 'state_dir / "invocations" / f"{uuid4()}.json"' in journal
    for field in (
        "schema_version",
        "upstream_session_id",
        "codex_thread_id",
        "resume",
        "source_model",
        "target_model",
        "reasoning_effort",
        "return_code",
        "terminal_status",
        "usage",
        "estimated_cost_usd",
        "cost_status",
        "duration_ms",
    ):
        assert f'"{field}":' in journal
    for forbidden in ("request.prompt", "run.final_message", "run.stderr", "API_KEY"):
        assert forbidden not in journal
