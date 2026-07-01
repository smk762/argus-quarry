from __future__ import annotations

import pytest

from argus_quarry.models import (
    Person,
    is_accepted_licence,
    normalise_licence,
    slugify_name,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("CC0", "CC0"),
        ("CC0 1.0", "CC0"),
        ("Public domain", "PD"),
        ("PD-US", "PD"),
        ("PD-Art (PD-old-100)", "PD"),
        ("No known copyright restrictions", "PD"),
        ("CC BY-SA 4.0", None),
        ("All rights reserved", None),
        ("", None),
        (None, None),
    ],
)
def test_normalise_licence(raw, expected):
    assert normalise_licence(raw) == expected
    assert is_accepted_licence(raw) is (expected is not None)


def test_slugify_name():
    assert slugify_name("Albert Einstein") == "Albert_Einstein"
    assert slugify_name("  Marie   Curie ") == "Marie_Curie"
    assert slugify_name("Q.-name!!") == "Q_name"


def test_person_folder():
    assert Person(name="Mark Twain").folder == "Mark_Twain"
