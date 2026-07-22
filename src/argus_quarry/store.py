"""SQLite provenance database — ``subjects`` + ``photographs`` (WAL mode).

Provenance-first and deliberately CV-free (no ``quality`` table — that is
argus-curator's job). ``photographs.sha256`` is ``UNIQUE`` so exact-duplicate
ingest is a no-op and reruns are idempotent. ``status`` tracks resumability.
Subjects carry a ``category`` (identity / wardrobe / setting / concept) so a
single pool can serve multiple LoRA-training workflows. WAL mode gives one
writer plus safe concurrent readers.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import pathname2url

from argus_quarry.models import DEFAULT_CATEGORY, Photograph, Subject, normalise_category

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT 'identity',
    wikidata_id  TEXT,
    birth_year   INTEGER,
    death_year   INTEGER,
    occupation   TEXT,
    UNIQUE(name, category)
);

CREATE TABLE IF NOT EXISTS photographs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id     INTEGER NOT NULL REFERENCES subjects(id),
    category       TEXT NOT NULL DEFAULT 'identity',
    title          TEXT,
    photographer   TEXT,
    year           INTEGER,
    source         TEXT NOT NULL,
    source_url     TEXT,
    licence        TEXT,
    attribution    TEXT,
    width          INTEGER,
    height         INTEGER,
    file_size      INTEGER,
    filename       TEXT,
    sha256         TEXT UNIQUE,
    phash          TEXT,
    remote_url     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    downloaded_at  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_photographs_remote ON photographs(remote_url);
CREATE INDEX IF NOT EXISTS idx_photographs_subject ON photographs(subject_id);
CREATE INDEX IF NOT EXISTS idx_photographs_status ON photographs(status);
CREATE INDEX IF NOT EXISTS idx_photographs_source ON photographs(source);
CREATE INDEX IF NOT EXISTS idx_photographs_licence ON photographs(licence);
CREATE INDEX IF NOT EXISTS idx_photographs_category ON photographs(category);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ProvenanceStore:
    """Thin wrapper around the SQLite provenance DB."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False, immutable: bool = False) -> None:
        """Open the provenance DB.

        ``read_only`` opens an existing DB through a ``mode=ro`` URI and skips
        both the ``mkdir`` and the migration, so a pool mounted ``:ro`` can be
        served (issue #5). SQLite still wants the *directory* writable for the
        WAL sidecars in that mode; ``immutable`` additionally promises the file
        never changes, which drops the sidecars entirely — correct for a
        genuinely read-only mount, wrong while a ``fetch`` is writing.
        """
        self.db_path = Path(db_path)
        self.read_only = read_only or immutable
        if self.read_only:
            if not self.db_path.is_file():
                raise FileNotFoundError(f"no provenance database at {self.db_path}")
            uri = f"file:{pathname2url(str(self.db_path.resolve()))}?mode=ro"
            if immutable:
                uri += "&immutable=1"
            self._conn = sqlite3.connect(uri, uri=True)
            self._conn.row_factory = sqlite3.Row
            self._probe()
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
            self._probe()
        except Exception:
            # A connect() to an unwritable DB only fails once it touches the file.
            self._conn.close()
            raise

    def _probe(self) -> None:
        """Fail fast if this connection cannot actually read.

        ``connect()`` is lazy and a WAL database wants to write its ``-shm``
        sidecar before the first read, so an unusable connection otherwise only
        surfaces mid-request. Closes itself before re-raising so the caller can
        fall back to a stricter open mode.
        """
        try:
            self._conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        except Exception:
            self._conn.close()
            raise

    # ── schema / migration ──────────────────────────────────────────
    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone()
        return row is not None

    def _column_exists(self, table: str, column: str) -> bool:
        return any(r["name"] == column for r in self._conn.execute(f"PRAGMA table_info({table})"))

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        for name, coldef in columns.items():
            if not self._column_exists(table, name):
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coldef}")

    def _subjects_unique_is_name_only(self) -> bool:
        """True if `subjects` carries the pre-0.2 UNIQUE(name) instead of UNIQUE(name, category)."""
        for idx in self._conn.execute("PRAGMA index_list(subjects)"):
            if idx["unique"]:
                cols = [r["name"] for r in self._conn.execute(f"PRAGMA index_info('{idx['name']}')")]
                if cols == ["name"]:
                    return True
        return False

    def _rebuild_subjects_unique(self) -> None:
        """Swap the legacy UNIQUE(name) for UNIQUE(name, category).

        A column-level UNIQUE can't be dropped in place in SQLite, so recreate the
        table (ids preserved). Done with FK enforcement off since `photographs`
        references `subjects(id)` and those ids are carried over unchanged.
        """
        self._conn.commit()
        self._conn.execute("PRAGMA foreign_keys=OFF")
        self._conn.executescript(
            """
            CREATE TABLE subjects_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                category     TEXT NOT NULL DEFAULT 'identity',
                wikidata_id  TEXT,
                birth_year   INTEGER,
                death_year   INTEGER,
                occupation   TEXT,
                UNIQUE(name, category)
            );
            INSERT INTO subjects_new (id, name, category, wikidata_id, birth_year, death_year, occupation)
                SELECT id, name, category, wikidata_id, birth_year, death_year, occupation FROM subjects;
            DROP TABLE subjects;
            ALTER TABLE subjects_new RENAME TO subjects;
            """
        )
        self._conn.execute("PRAGMA foreign_keys=ON")

    def _relocate_legacy_files(self) -> None:
        """Move landed files from flat `images/<subject>/` into `images/<category>/<subject>/`.

        Legacy pools stored every file directly under the subject folder; the new
        layout sorts by category. Relocate per recorded row so `verify`/`export`
        (which build `images/<category>/<subject>/…`) keep resolving after upgrade.
        """
        images_dir = self.db_path.parent.parent / "images"
        if not images_dir.exists():
            return
        rows = self._conn.execute(
            "SELECT ph.category AS category, ph.filename AS filename, sub.name AS subject "
            "FROM photographs ph JOIN subjects sub ON sub.id = ph.subject_id "
            "WHERE ph.filename IS NOT NULL"
        ).fetchall()
        for row in rows:
            old = images_dir / row["subject"] / row["filename"]
            new = images_dir / row["category"] / row["subject"] / row["filename"]
            if old.exists() and not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                old.replace(new)

    def _migrate(self) -> None:
        # Legacy (pre-0.2) schema used `people`/`person_id`, no category, fewer
        # photograph columns, a flat image layout, and UNIQUE(name). Rename/extend
        # in place so existing pools keep their provenance, then let the CREATE
        # TABLE IF NOT EXISTS below cover fresh installs.
        legacy = self._table_exists("people") and not self._table_exists("subjects")
        if legacy:
            self._conn.execute("ALTER TABLE people RENAME TO subjects")
        if self._table_exists("subjects"):
            self._ensure_columns(
                "subjects",
                {
                    "category": "TEXT NOT NULL DEFAULT 'identity'",
                    "wikidata_id": "TEXT",
                    "birth_year": "INTEGER",
                    "death_year": "INTEGER",
                    "occupation": "TEXT",
                },
            )
            if self._subjects_unique_is_name_only():
                self._rebuild_subjects_unique()
        if self._table_exists("photographs"):
            if self._column_exists("photographs", "person_id") and not self._column_exists("photographs", "subject_id"):
                self._conn.execute("ALTER TABLE photographs RENAME COLUMN person_id TO subject_id")
            self._ensure_columns(
                "photographs",
                {
                    "category": "TEXT NOT NULL DEFAULT 'identity'",
                    "title": "TEXT",
                    "photographer": "TEXT",
                    "year": "INTEGER",
                    "source": "TEXT",
                    "source_url": "TEXT",
                    "licence": "TEXT",
                    "attribution": "TEXT",
                    "width": "INTEGER",
                    "height": "INTEGER",
                    "file_size": "INTEGER",
                    "filename": "TEXT",
                    "sha256": "TEXT",
                    "phash": "TEXT",
                    "status": "TEXT NOT NULL DEFAULT 'pending'",
                    "downloaded_at": "TEXT",
                },
            )
        self._conn.executescript(_SCHEMA)
        if legacy:
            self._relocate_legacy_files()
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ProvenanceStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── subjects ────────────────────────────────────────────────────
    def upsert_subject(self, subject: Subject) -> int:
        """Insert or update a subject by (folder name, category); return its id."""
        name = subject.folder
        cur = self._conn.execute("SELECT id FROM subjects WHERE name = ? AND category = ?", (name, subject.category))
        row = cur.fetchone()
        if row is not None:
            self._conn.execute(
                """UPDATE subjects SET wikidata_id = COALESCE(?, wikidata_id),
                       birth_year = COALESCE(?, birth_year),
                       death_year = COALESCE(?, death_year),
                       occupation = COALESCE(?, occupation)
                   WHERE id = ?""",
                (subject.wikidata_id, subject.birth_year, subject.death_year, subject.occupation, row["id"]),
            )
            self._conn.commit()
            return int(row["id"])
        cur = self._conn.execute(
            "INSERT INTO subjects (name, category, wikidata_id, birth_year, death_year, occupation) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, subject.category, subject.wikidata_id, subject.birth_year, subject.death_year, subject.occupation),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def subject_name(self, subject_id: int) -> str | None:
        cur = self._conn.execute("SELECT name FROM subjects WHERE id = ?", (subject_id,))
        row = cur.fetchone()
        return row["name"] if row else None

    # ── photographs ─────────────────────────────────────────────────
    _PHOTO_SELECT = "SELECT ph.*, sub.name AS subject FROM photographs ph JOIN subjects sub ON sub.id = ph.subject_id"

    def get_by_remote_url(self, remote_url: str) -> Photograph | None:
        cur = self._conn.execute(self._PHOTO_SELECT + " WHERE ph.remote_url = ?", (remote_url,))
        return self._row_to_photo(cur.fetchone())

    def get_by_sha256(self, sha256: str) -> Photograph | None:
        cur = self._conn.execute(self._PHOTO_SELECT + " WHERE ph.sha256 = ?", (sha256,))
        return self._row_to_photo(cur.fetchone())

    def insert_pending(self, photo: Photograph) -> int:
        """Insert a row (status defaults to ``pending``) and return its id."""
        cur = self._conn.execute(
            """INSERT INTO photographs
                   (subject_id, category, title, photographer, year, source, source_url, licence,
                    attribution, remote_url, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                photo.subject_id,
                photo.category,
                photo.title,
                photo.photographer,
                photo.year,
                photo.source,
                photo.source_url,
                photo.licence,
                photo.attribution,
                photo.remote_url,
                photo.status or "pending",
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def set_status(self, photo_id: int, status: str) -> None:
        self._conn.execute("UPDATE photographs SET status = ? WHERE id = ?", (status, photo_id))
        self._conn.commit()

    def mark_complete(
        self,
        photo_id: int,
        *,
        filename: str,
        sha256: str,
        width: int | None,
        height: int | None,
        file_size: int,
        phash: str | None = None,
    ) -> None:
        self._conn.execute(
            """UPDATE photographs
                   SET status = 'complete', filename = ?, sha256 = ?, width = ?, height = ?,
                       file_size = ?, phash = ?, downloaded_at = ?
                   WHERE id = ?""",
            (filename, sha256, width, height, file_size, phash, _now(), photo_id),
        )
        self._conn.commit()

    def total_bytes(self, *, only_complete: bool = True) -> int:
        """Sum of landed file sizes — the raw-pool footprint (for the budget cap)."""
        q = "SELECT COALESCE(SUM(file_size), 0) AS total FROM photographs"
        if only_complete:
            q += " WHERE status = 'complete'"
        return int(self._conn.execute(q).fetchone()["total"])

    # ── queries for list / stats / export / verify / server ────────
    @staticmethod
    def _photo_where(
        *,
        status: str | None,
        source: str | None,
        licences: list[str] | None,
        subject: str | None,
        category: str | None,
    ) -> tuple[str, list[object]]:
        """Shared WHERE-clause builder for the photograph list/count queries."""
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("ph.status = ?")
            params.append(status)
        if source:
            clauses.append("ph.source = ?")
            params.append(source)
        if licences:
            placeholders = ",".join("?" for _ in licences)
            clauses.append(f"ph.licence IN ({placeholders})")
            params.extend(licences)
        if category:
            clauses.append("ph.category = ?")
            params.append(normalise_category(category))
        if subject:
            clauses.append("sub.name = ?")
            params.append(subject)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def iter_photographs(
        self,
        *,
        status: str | None = "complete",
        source: str | None = None,
        licences: list[str] | None = None,
        subject: str | None = None,
        category: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Photograph]:
        where, params = self._photo_where(
            status=status, source=source, licences=licences, subject=subject, category=category
        )
        q = self._PHOTO_SELECT + where + " ORDER BY ph.category, sub.name, ph.id"
        if limit is not None:
            q += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        return [self._row_to_photo(r) for r in self._conn.execute(q, params) if r is not None]

    def count_photographs(
        self,
        *,
        status: str | None = "complete",
        source: str | None = None,
        licences: list[str] | None = None,
        subject: str | None = None,
        category: str | None = None,
    ) -> int:
        """Number of photographs matching the same filters as :meth:`iter_photographs`."""
        where, params = self._photo_where(
            status=status, source=source, licences=licences, subject=subject, category=category
        )
        q = "SELECT COUNT(*) AS n FROM photographs ph JOIN subjects sub ON sub.id = ph.subject_id" + where
        return int(self._conn.execute(q, params).fetchone()["n"])

    def get_photograph(self, photo_id: int) -> Photograph | None:
        cur = self._conn.execute(self._PHOTO_SELECT + " WHERE ph.id = ?", (photo_id,))
        return self._row_to_photo(cur.fetchone())

    def subjects_with_counts(self, *, category: str | None = None) -> list[dict[str, object]]:
        """Distinct subjects with their landed (``complete``) photo counts."""
        where = ""
        params: list[object] = []
        if category:
            where = " WHERE sub.category = ?"
            params.append(normalise_category(category))
        q = (
            "SELECT sub.name AS folder, sub.category AS category, "
            "COUNT(CASE WHEN ph.status = 'complete' THEN 1 END) AS photo_count "
            "FROM subjects sub LEFT JOIN photographs ph ON ph.subject_id = sub.id"
            + where
            + " GROUP BY sub.id ORDER BY sub.category, sub.name"
        )
        return [
            {"folder": r["folder"], "category": r["category"], "photo_count": int(r["photo_count"])}
            for r in self._conn.execute(q, params)
        ]

    def counts_by(self, column: str, *, only_complete: bool = True) -> dict[str, int]:
        if column not in {"source", "licence", "status", "category"}:
            raise ValueError(f"unsupported group column: {column}")
        q = f"SELECT {column} AS k, COUNT(*) AS n FROM photographs"
        if only_complete:
            q += " WHERE status = 'complete'"
        q += f" GROUP BY {column}"
        return {(r["k"] or "?"): int(r["n"]) for r in self._conn.execute(q)}

    def stats(self) -> dict[str, object]:
        total = int(self._conn.execute("SELECT COUNT(*) AS n FROM photographs").fetchone()["n"])
        subjects = int(self._conn.execute("SELECT COUNT(*) AS n FROM subjects").fetchone()["n"])
        return {
            "subjects": subjects,
            "photographs": total,
            "by_status": self.counts_by("status", only_complete=False),
            "by_category": self.counts_by("category"),
            "by_source": self.counts_by("source"),
            "by_licence": self.counts_by("licence"),
            "total_bytes": self.total_bytes(),
        }

    @staticmethod
    def _row_to_photo(row: sqlite3.Row | None) -> Photograph | None:
        if row is None:
            return None
        keys = set(row.keys())
        return Photograph(
            id=row["id"],
            subject_id=row["subject_id"],
            subject=row["subject"] if "subject" in keys else "",
            category=row["category"] if "category" in keys else DEFAULT_CATEGORY,
            title=row["title"],
            photographer=row["photographer"],
            year=row["year"],
            source=row["source"],
            source_url=row["source_url"],
            licence=row["licence"],
            attribution=row["attribution"],
            width=row["width"],
            height=row["height"],
            file_size=row["file_size"],
            filename=row["filename"],
            sha256=row["sha256"],
            phash=row["phash"],
            remote_url=row["remote_url"],
            status=row["status"],
            downloaded_at=row["downloaded_at"],
        )
