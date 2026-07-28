from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).parents[1]
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
    "SKILL.md": "3cd86ea1435461c96ee167b1a1ec316a0f764cf0eec9d6a2d093cfd35364b539",
    "references/api.md": (
        "802f9bba2c0ce46d9411a2c3dd5488764ecac1d3f23dd32d780936e4724558d7"
    ),
    "references/gotchas.md": (
        "bc2ea2de65332f453ada5d955a07d9f7a46e231735c14037d31d81f2586d5187"
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


def test_upstream_copy_has_no_known_truncations() -> None:
    copied_text = "\n".join(
        path.read_text(encoding="utf-8") for path in SKILL.rglob("*.md")
    )
    found = sorted(fragment.strip() for fragment in KNOWN_TRUNCATIONS if fragment in copied_text)
    assert found == []
