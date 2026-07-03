from __future__ import annotations

import sqlite3

from argus_quarry.models import Photograph, Subject
from argus_quarry.store import ProvenanceStore


def test_upsert_subject_is_idempotent_and_merges(config):
    with ProvenanceStore(config.db_path) as store:
        pid1 = store.upsert_subject(Subject(name="Albert Einstein"))
        pid2 = store.upsert_subject(Subject(name="Albert Einstein", wikidata_id="Q937", death_year=1955))
        assert pid1 == pid2
        assert store.subject_name(pid1) == "Albert_Einstein"


def test_same_name_distinct_categories_are_separate_subjects(config):
    with ProvenanceStore(config.db_path) as store:
        a = store.upsert_subject(Subject(name="Nike", category="identity"))
        b = store.upsert_subject(Subject(name="Nike", category="wardrobe"))
        assert a != b


def _photo(
    subject_id: int, url: str, *, category: str = "identity", subject: str = "Albert_Einstein", **kw
) -> Photograph:
    base = dict(
        subject_id=subject_id,
        subject=subject,
        category=category,
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
        pid = store.upsert_subject(Subject(name="Albert Einstein"))
        rid = store.insert_pending(_photo(pid, "https://img/1.jpg"))
        assert store.get_by_remote_url("https://img/1.jpg").id == rid

        store.mark_complete(rid, filename="a.jpg", sha256="deadbeef", width=10, height=10, file_size=1234)
        assert store.get_by_sha256("deadbeef").id == rid
        assert store.total_bytes() == 1234


def test_stats_and_counts(config):
    with ProvenanceStore(config.db_path) as store:
        pid = store.upsert_subject(Subject(name="Albert Einstein"))
        wid = store.upsert_subject(Subject(name="Kimono", category="wardrobe"))
        a = store.insert_pending(_photo(pid, "https://img/a.jpg", licence="CC0"))
        b = store.insert_pending(_photo(pid, "https://img/b.jpg", licence="PD"))
        c = store.insert_pending(_photo(wid, "https://img/c.jpg", licence="CC0", category="wardrobe", subject="Kimono"))
        store.mark_complete(a, filename="a.jpg", sha256="aa", width=1, height=1, file_size=100)
        store.mark_complete(b, filename="b.jpg", sha256="bb", width=1, height=1, file_size=200)
        store.mark_complete(c, filename="c.jpg", sha256="cc", width=1, height=1, file_size=50)

        s = store.stats()
        assert s["subjects"] == 2
        assert s["photographs"] == 3
        assert s["total_bytes"] == 350
        assert s["by_licence"] == {"CC0": 2, "PD": 1}
        assert s["by_category"] == {"identity": 2, "wardrobe": 1}


def test_migrates_legacy_people_schema(config):
    # Simulate a pre-0.2 DB: `people` table + `photographs.person_id`, no category.
    db_path = config.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript(
        """
        CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            wikidata_id TEXT, birth_year INTEGER, death_year INTEGER, occupation TEXT);
        CREATE TABLE photographs (id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL,
            source TEXT, source_url TEXT, licence TEXT, remote_url TEXT NOT NULL,
            file_size INTEGER, filename TEXT, sha256 TEXT, status TEXT NOT NULL DEFAULT 'complete');
        INSERT INTO people (id, name) VALUES (1, 'Albert_Einstein');
        INSERT INTO photographs (person_id, source, source_url, licence, remote_url, file_size, filename, sha256)
            VALUES (1, 'commons', 'https://x', 'PD', 'https://img/legacy.jpg', 10, 'legacy.jpg', 'legacyhash');
        """
    )
    con.commit()
    con.close()

    # A legacy landed file sits under the flat images/<subject>/ layout.
    legacy_file = config.images_dir / "Albert_Einstein" / "legacy.jpg"
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_bytes(b"x" * 10)

    with ProvenanceStore(db_path) as store:
        # Legacy row survives with a default identity category.
        rows = store.iter_photographs(status="complete")
        assert len(rows) == 1
        assert rows[0].subject == "Albert_Einstein"
        assert rows[0].category == "identity"
        assert store.total_bytes() == 10
        # File was relocated into the category-sorted layout.
        assert not legacy_file.exists()
        assert (config.images_dir / "identity" / "Albert_Einstein" / "legacy.jpg").exists()
        # Migrated DB accepts the same slug in a second category (UNIQUE(name, category)).
        einstein_identity = store.upsert_subject(Subject(name="Albert Einstein", category="identity"))
        einstein_concept = store.upsert_subject(Subject(name="Albert Einstein", category="concept"))
        assert einstein_identity != einstein_concept
        wid = store.upsert_subject(Subject(name="Kimono", category="wardrobe"))
        assert store.subject_name(wid) == "Kimono"


def test_iter_photographs_category_filter_is_case_insensitive(config):
    with ProvenanceStore(config.db_path) as store:
        wid = store.upsert_subject(Subject(name="Kimono", category="wardrobe"))
        c = store.insert_pending(_photo(wid, "https://img/c.jpg", category="wardrobe", subject="Kimono"))
        store.mark_complete(c, filename="c.jpg", sha256="cc", width=1, height=1, file_size=1)
        # Mixed-case filter still matches the lower-cased stored category.
        assert len(store.iter_photographs(category="Wardrobe")) == 1
        assert len(store.iter_photographs(category="WARDROBE")) == 1


def test_iter_photographs_filters_by_category(config):
    with ProvenanceStore(config.db_path) as store:
        pid = store.upsert_subject(Subject(name="Albert Einstein"))
        wid = store.upsert_subject(Subject(name="Kimono", category="wardrobe"))
        a = store.insert_pending(_photo(pid, "https://img/a.jpg"))
        c = store.insert_pending(_photo(wid, "https://img/c.jpg", category="wardrobe", subject="Kimono"))
        store.mark_complete(a, filename="a.jpg", sha256="aa", width=1, height=1, file_size=1)
        store.mark_complete(c, filename="c.jpg", sha256="cc", width=1, height=1, file_size=1)

        wardrobe = store.iter_photographs(category="wardrobe")
        assert [p.subject for p in wardrobe] == ["Kimono"]
        assert wardrobe[0].category == "wardrobe"
