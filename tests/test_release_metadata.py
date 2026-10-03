import runpy
from pathlib import Path

import pytest

from booking_bot.version import __version__

HEADINGS = ("Added", "Changed", "Fixed", "Migration notes", "Breaking changes")
release_notes = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/release_metadata.py")
)["release_notes"]


def changelog(tmp_path, monkeypatch, headings=HEADINGS):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        f"## [{__version__}] - 2026-10-01\n"
        + "\n".join(f"### {h}\n- Reviewed change" for h in headings)
        + "\n## [0.0.1]\nOld release\n",
        encoding="utf-8",
    )


def test_matching_release_extracts_only_its_notes(tmp_path, monkeypatch):
    changelog(tmp_path, monkeypatch)
    notes = release_notes(f"v{__version__}")
    assert all(f"### {h}" in notes for h in HEADINGS)
    assert "Old release" not in notes


@pytest.mark.parametrize("tag", ["0.6.0", "v99.0.0", "v0.6.0-rc1"])
def test_mismatched_tag_is_rejected(tag):
    with pytest.raises(ValueError, match="tag"):
        release_notes(tag)


@pytest.mark.parametrize("missing", HEADINGS)
def test_incomplete_release_notes_are_rejected(tmp_path, monkeypatch, missing):
    changelog(tmp_path, monkeypatch, tuple(h for h in HEADINGS if h != missing))
    with pytest.raises(ValueError, match="heading"):
        release_notes(f"v{__version__}")


@pytest.fixture
def stable_notes(tmp_path, monkeypatch):
    changelog(tmp_path, monkeypatch)
    path = tmp_path / "CHANGELOG.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(__version__, "1.0.0"), encoding="utf-8"
    )
    monkeypatch.setitem(release_notes.__globals__, "__version__", "1.0.0")
    notes = tmp_path / "docs/releases/v1.0.0-notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text(
        "\n".join(
            f"## {heading}\nReviewed content"
            for heading in (
                "Main features",
                "Production requirements",
                "Upgrade notes",
                "Known limitations",
            )
        ),
        encoding="utf-8",
    )
    return notes


def test_stable_release_uses_user_oriented_notes(stable_notes):
    assert release_notes("v1.0.0").strip() == stable_notes.read_text(encoding="utf-8").strip()


def test_draft_stable_notes_block_publication(stable_notes):
    stable_notes.write_text(
        "[DRAFT]\n" + stable_notes.read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="qualification"):
        release_notes("v1.0.0")


@pytest.mark.parametrize(
    "missing", ["Main features", "Production requirements", "Upgrade notes", "Known limitations"]
)
def test_stable_notes_cannot_hide_requirements_or_limitations(stable_notes, missing):
    stable_notes.write_text(
        stable_notes.read_text(encoding="utf-8").replace(f"## {missing}", "## Removed"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="heading"):
        release_notes("v1.0.0")
