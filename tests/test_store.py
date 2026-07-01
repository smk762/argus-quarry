from __future__ import annotations

from argus_quarry.models import Person, Photograph
from argus_quarry.store import ProvenanceStore


def test_upsert_person_is_idempotent_and_merges(config):
    with ProvenanceStore(config.db_path) as store:
        pid1 = store.upsert_person(Person(name="Albert Einstein"))
        pid2 = store.upsert_person(Person(name="Albert Einstein", wikidata_id="Q937", death_year=1955))
        assert pid1 == pid2
        assert store.person_name(pid1) == "Albert_Einstein"


def _photo(person_id: int, url: str, **kw) -> Photograph:
    base = dict(
        person_id=person_id,
        person_name="Albert_Einstein",
        source="commons",
        source_url="https://example.org",
        licence="PD",
        remote_url=url,
        status="pending",
    )
    base.update(kw)
    return Photograph(**base)


def test_insert_and_lookup_by_remote_and_sha(config):
    with ProvenanceStore(config.db_path) as store:
        pid = store.upsert_person(Person(name="Albert Einstein"))
        rid = store.insert_pending(_photo(pid, "https://img/1.jpg"))
        assert store.get_by_remote_url("https://img/1.jpg").id == rid

        store.mark_complete(rid, filename="a.jpg", sha256="deadbeef", width=10, height=10, file_size=1234)
        assert store.get_by_sha256("deadbeef").id == rid
        assert store.total_bytes() == 1234


def test_stats_and_counts(config):
    with ProvenanceStore(config.db_path) as store:
        pid = store.upsert_person(Person(name="Albert Einstein"))
        a = store.insert_pending(_photo(pid, "https://img/a.jpg", licence="CC0"))
        b = store.insert_pending(_photo(pid, "https://img/b.jpg", licence="PD"))
        store.mark_complete(a, filename="a.jpg", sha256="aa", width=1, height=1, file_size=100)
        store.mark_complete(b, filename="b.jpg", sha256="bb", width=1, height=1, file_size=200)

        s = store.stats()
        assert s["people"] == 1
        assert s["photographs"] == 2
        assert s["total_bytes"] == 300
        assert s["by_licence"] == {"CC0": 1, "PD": 1}
