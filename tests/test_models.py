from __future__ import annotations

import pytest

from argus_quarry.models import (
    DEFAULT_CATEGORY,
    Person,
    Subject,
    is_accepted_licence,
    normalise_category,
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
        ("https://creativecommons.org/publicdomain/mark/1.0/", "PD"),
        ("public domain or equivalent (Megalith-10m filter: Flickr PDM / CC-zero / Flickr Commons / US Gov)", "PD"),
        # negated / qualified labels name a free licence without granting it
        ("Not CC0", None),
        ("non-cc0", None),
        ("CC0 not applicable", None),
        ("formerly CC0, now CC BY-NC", None),
        ("not public domain in the US", None),
        ("public domain status unknown", None),
        # tokens match whole words only: "pd" is not in "updated"
        ("CC BY-SA, updated 2020", None),
        ("Proprietary (rpd licence)", None),
        # issue #40's three false accepts
        ("Copyrighted, updated 2019", None),
        ("CC BY-SA 4.0; GPDL", None),
        ("All rights reserved (see PDF)", None),
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
    # Person is kept as a backward-compatible alias of Subject.
    assert Person is Subject
    assert Person(name="Mark Twain").folder == "Mark_Twain"


def test_subject_category_and_query():
    # category defaults to identity and is normalised to lower-case.
    assert Subject(name="Mark Twain").category == DEFAULT_CATEGORY
    assert Subject(name="Red dress", category="Wardrobe").category == "wardrobe"

    # query falls back to the name, or uses an explicit search string.
    assert Subject(name="Kimono").query == "Kimono"
    assert Subject(name="Kimono", search="kimono garment").query == "kimono garment"

    # non-identity subjects still slugify to a folder name.
    assert Subject(name="Red dress", category="wardrobe").folder == "Red_dress"


@pytest.mark.parametrize(
    "raw,expected",
    [("Wardrobe", "wardrobe"), ("  Setting ", "setting"), ("", "identity"), (None, "identity")],
)
def test_normalise_category(raw, expected):
    assert normalise_category(raw) == expected


def test_subject_queries_put_the_primary_first_then_unique_aliases():
    from argus_quarry.models import Subject

    s = Subject(name="Mark Twain", aliases=["Samuel Clemens", "MARK TWAIN", "", "Samuel Clemens"])
    assert s.queries == ["Mark Twain", "Samuel Clemens"]
    assert Subject(name="Red dress", search="red dress fashion").queries == ["red dress fashion"]


def test_pre_0_2_aliases_stay_importable():
    import argus_quarry
    from argus_quarry.subjects import load_people

    assert argus_quarry.PortraitRecord is argus_quarry.SourceRecord and argus_quarry.Person is Subject
    assert callable(load_people)
