"""Validate source version/tag/changelog before publishing anything."""

import argparse
import os
import re
from pathlib import Path

from booking_bot.version import __version__, parse_release_version


def release_notes(tag: str) -> str:
    if not tag.startswith("v") or tag[1:] != __version__:
        raise ValueError("Release tag must match version.py exactly")
    parse_release_version(tag[1:])
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    section = re.search(
        rf"^## \[{re.escape(__version__)}\][^\n]*\n(.*?)(?=^## |\Z)",
        changelog,
        re.M | re.S,
    )
    if not section:
        raise ValueError("Release needs its own CHANGELOG.md section")
    notes = section[1].strip()
    for heading in ("Added", "Changed", "Fixed", "Migration notes", "Breaking changes"):
        if f"### {heading}" not in notes:
            raise ValueError(f"Missing changelog heading: {heading}")
    if __version__ == "1.0.0":
        notes = Path("docs/releases/v1.0.0-notes.md").read_text(encoding="utf-8")
        if "[DRAFT]" in notes:
            raise ValueError("Stable release notes are still a draft; close qualification gates")
        for heading in (
            "Main features",
            "Production requirements",
            "Upgrade notes",
            "Known limitations",
        ):
            if f"## {heading}" not in notes:
                raise ValueError(f"Missing release notes heading: {heading}")
    return notes + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag")
    parser.add_argument("--notes", type=Path)
    args = parser.parse_args()
    notes = release_notes(args.tag) if args.tag else None
    if args.notes and notes:
        args.notes.write_text(notes, encoding="utf-8")
    output = (
        f"version={__version__}\nminor={'.'.join(__version__.split('.')[:2])}\n"
        f"prerelease={'true' if '-rc.' in __version__ else 'false'}\n"
    )
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(output)
    print(output, end="")
