#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["packaging>=23"]
# ///
"""Check that CHANGELOG.md is ready to release.

Two modes, both run in CI:

  --release-pr        For a PR into main (a release). [Unreleased] must be empty,
                      and the newest version section must be newer than the latest
                      v* tag, dated, non-empty and have its compare link. That is
                      the section the tag's GitHub Release will use as its notes.
  --tag vX.Y.Z        For a pushed tag, before anything publishes. CHANGELOG.md
                      must have a non-empty section for X.Y.Z.

Exits 1 with a GitHub ::error:: line per problem.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

HEADING = re.compile(r"^## \[(?P<name>[^\]]+)\](?:\s*-\s*(?P<date>\d{4}-\d{2}-\d{2}))?\s*$")
LINK_REF = re.compile(r"^\[(?P<name>[^\]]+)\]:\s*\S+")


def parse(text: str) -> tuple[list[tuple[str, str | None, str]], set[str]]:
    """Return ([(name, date, body)], link-reference names), sections in file order."""
    sections: list[tuple[str, str | None, list[str]]] = []
    links: set[str] = set()
    for line in text.splitlines():
        if m := LINK_REF.match(line):
            links.add(m["name"])  # the link block closes the file, ending the last section
            continue
        if m := HEADING.match(line):
            sections.append((m["name"], m["date"], []))
        elif sections and not links:
            sections[-1][2].append(line)
    return [(n, d, "\n".join(b).strip()) for n, d, b in sections], links


def latest_tag() -> Version | None:
    out = subprocess.run(["git", "tag", "--list", "v*"], capture_output=True, text=True, check=True).stdout
    versions = []
    for tag in out.split():
        try:
            versions.append(Version(tag.removeprefix("v")))
        except InvalidVersion:
            continue
    return max(versions, default=None)


def check_release_pr(text: str, latest: Version | None) -> list[str]:
    sections, links = parse(text)
    errors: list[str] = []
    released = [s for s in sections if s[0] != "Unreleased"]
    unreleased = [s for s in sections if s[0] == "Unreleased"]
    if unreleased and unreleased[0][2]:
        errors.append("[Unreleased] still has entries; cut them into a version section before releasing")
    if not released:
        return [*errors, "CHANGELOG.md has no version section"]
    name, date, body = released[0]
    try:
        version = Version(name)
    except InvalidVersion:
        return [*errors, f"newest section [{name}] is not a version"]
    if latest is not None and version <= latest:
        errors.append(
            f"newest section [{name}] is not newer than the latest tag v{latest}; add a section for this release"
        )
    if not date:
        errors.append(f"section [{name}] has no date (`## [{name}] - YYYY-MM-DD`)")
    if not body:
        errors.append(f"section [{name}] is empty")
    if name not in links:
        errors.append(f"no `[{name}]: ` compare link at the end of CHANGELOG.md")
    return errors


def check_tag(text: str, tag: str) -> list[str]:
    version = tag.removeprefix("v")
    for name, _date, body in parse(text)[0]:
        if name == version:
            return [] if body else [f"CHANGELOG.md section [{version}] is empty"]
    return [f"CHANGELOG.md has no section for {version}"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--release-pr", action="store_true", help="check a release PR into main")
    mode.add_argument("--tag", help="check the section for this tag (vX.Y.Z)")
    ap.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    args = ap.parse_args(argv)

    text = args.changelog.read_text(encoding="utf-8")
    errors = check_tag(text, args.tag) if args.tag else check_release_pr(text, latest_tag())
    for err in errors:
        print(f"::error file={args.changelog}::{err}")
    if not errors:
        print("CHANGELOG.md is ready to release")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
