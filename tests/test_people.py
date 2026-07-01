from __future__ import annotations

import pytest

from argus_quarry.people import load_people, load_seed


def test_load_packaged_seed():
    people = load_seed()
    assert len(people) >= 3
    names = {p.name for p in people}
    assert "Albert Einstein" in names
    einstein = next(p for p in people if p.name == "Albert Einstein")
    assert einstein.wikidata_id == "Q937"
    assert einstein.folder == "Albert_Einstein"


def test_load_people_limit():
    assert len(load_people(limit=2)) == 2


def test_from_wikidata_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        load_people(from_wikidata=True)
