from __future__ import annotations

import pytest

from argus_quarry.models import SUBJECT_CATEGORIES
from argus_quarry.subjects import load_category, load_people, load_subjects


def test_load_identity_category():
    people = load_category("identity")
    assert len(people) >= 3
    names = {p.name for p in people}
    assert "Albert Einstein" in names
    einstein = next(p for p in people if p.name == "Albert Einstein")
    assert einstein.wikidata_id == "Q937"
    assert einstein.folder == "Albert_Einstein"
    assert all(p.category == "identity" for p in people)


def test_each_category_has_a_packaged_seed():
    for category in SUBJECT_CATEGORIES:
        subjects = load_category(category)
        assert subjects, f"no seeds for {category}"
        assert all(s.category == category for s in subjects)


def test_load_all_subjects_spans_every_category():
    subjects = load_subjects()
    categories = {s.category for s in subjects}
    assert categories == set(SUBJECT_CATEGORIES)


def test_load_subjects_single_category_and_limit():
    wardrobe = load_subjects(category="wardrobe")
    assert wardrobe and all(s.category == "wardrobe" for s in wardrobe)
    assert len(load_subjects(limit=2)) == 2


def test_load_people_alias_is_identity_only():
    people = load_people()
    assert people and all(p.category == "identity" for p in people)


def test_from_wikidata_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        load_subjects(from_wikidata=True)
