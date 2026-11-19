"""scripts/check_changelog.py: the release-readiness gate for CHANGELOG.md."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from packaging.version import Version

_SPEC = importlib.util.spec_from_file_location(
    "check_changelog", Path(__file__).parents[1] / "scripts" / "check_changelog.py"
)
cc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cc)

READY = """# Changelog

## [Unreleased]

## [0.4.0] - 2026-11-14

### Added

- Something new.

## [0.3.1] - 2026-10-24

### Fixed

- Something broken.

[Unreleased]: https://example.test/compare/v0.4.0...HEAD
[0.4.0]: https://example.test/compare/v0.3.1...v0.4.0
[0.3.1]: https://example.test/compare/v0.3.0...v0.3.1
"""

# The v0.4.0 mistake: everything left under [Unreleased] at release time.
NOT_CUT = """# Changelog

## [Unreleased]

### Added

- Something new.

## [0.3.1] - 2026-10-24

- Something broken.

[0.3.1]: https://example.test/compare/v0.3.0...v0.3.1
"""


def test_ready_changelog_passes():
    assert cc.check_release_pr(READY, Version("0.3.1")) == []
    assert cc.check_release_pr(READY, None) == []


def test_uncut_unreleased_fails():
    errors = cc.check_release_pr(NOT_CUT, Version("0.3.1"))
    assert any("[Unreleased] still has entries" in e for e in errors)
    assert any("not newer than the latest tag v0.3.1" in e for e in errors)


def test_already_tagged_newest_section_fails():
    errors = cc.check_release_pr(READY, Version("0.4.0"))
    assert errors == ["newest section [0.4.0] is not newer than the latest tag v0.4.0; add a section for this release"]


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (("## [0.4.0] - 2026-11-14", "## [0.4.0]"), "has no date"),
        (("[0.4.0]: https://example.test/compare/v0.3.1...v0.4.0\n", ""), "compare link"),
        (("### Added\n\n- Something new.\n", ""), "is empty"),
    ],
)
def test_incomplete_section_fails(edit, expected):
    errors = cc.check_release_pr(READY.replace(*edit), Version("0.3.1"))
    assert len(errors) == 1 and expected in errors[0]


def test_link_block_is_not_part_of_the_oldest_section():
    sections, links = cc.parse(READY)
    assert sections[-1] == ("0.3.1", "2026-10-24", "### Fixed\n\n- Something broken.")
    assert links == {"Unreleased", "0.4.0", "0.3.1"}


def test_tag_mode():
    assert cc.check_tag(READY, "v0.4.0") == []
    assert cc.check_tag(NOT_CUT, "v0.4.0") == ["CHANGELOG.md has no section for 0.4.0"]


def test_real_changelog_has_every_tagged_release():
    text = (Path(__file__).parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    assert cc.check_tag(text, "v0.4.0") == []
