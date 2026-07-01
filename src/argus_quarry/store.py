"""SQLite provenance database — ``people`` + ``photographs`` (WAL mode).

Provenance-first and deliberately CV-free (no ``quality`` table — that is
argus-curator's job). ``photographs.sha256`` is ``UNIQUE`` so exact-duplicate
ingest is a no-op and reruns are idempotent. ``status`` tracks resumability.
WAL mode gives one writer plus safe concurrent readers.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from argus_quarry.models import Person, Photograph

_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    wikidata_id  TEXT,
    birth_year   INTEGER,
    death_year   INTEGER,
    occupation   TEXT
);

CREATE TABLE IF NOT EXISTS photographs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id      INTEGER NOT NULL REFERENCES people(id),
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
CREATE INDEX IF NOT EXISTS idx_photographs_person ON photographs(person_id);
CREATE INDEX IF NOT EXISTS idx_photographs_status ON photographs(status);
CREATE INDEX IF NOT EXISTS idx_photographs_source ON photographs(source);
CREATE INDEX IF NOT EXISTS idx_photographs_licence ON photographs(licence);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ProvenanceStore:
    """Thin wrapper around the SQLite provenance DB."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ProvenanceStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── people ──────────────────────────────────────────────────────
    def upsert_person(self, person: Person) -> int:
        """Insert or update a person by canonical folder name; return its id."""
        name = person.folder
        cur = self._conn.execute("SELECT id FROM people WHERE name = ?", (name,))
        row = cur.fetchone()
        if row is not None:
            self._conn.execute(
                """UPDATE people SET wikidata_id = COALESCE(?, wikidata_id),
                       birth_year = COALESCE(?, birth_year),
                       death_year = COALESCE(?, death_year),
                       occupation = COALESCE(?, occupation)
                   WHERE id = ?""",
                (person.wikidata_id, person.birth_year, person.death_year, person.occupation, row["id"]),
            )
            self._conn.commit()
            return int(row["id"])
        cur = self._conn.execute(
            "INSERT INTO people (name, wikidata_id, birth_year, death_year, occupation) VALUES (?, ?, ?, ?, ?)",
            (name, person.wikidata_id, person.birth_year, person.death_year, person.occupation),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def person_name(self, person_id: int) -> str | None:
        cur = self._conn.execute("SELECT name FROM people WHERE id = ?", (person_id,))
        row = cur.fetchone()
        return row["name"] if row else None

    # ── photographs ─────────────────────────────────────────────────
    def get_by_remote_url(self, remote_url: str) -> Photograph | None:
        cur = self._conn.execute("SELECT * FROM photographs WHERE remote_url = ?", (remote_url,))
        return self._row_to_photo(cur.fetchone())

    def get_by_sha256(self, sha256: str) -> Photograph | None:
        cur = self._conn.execute("SELECT * FROM photographs WHERE sha256 = ?", (sha256,))
        return self._row_to_photo(cur.fetchone())

    def insert_pending(self, photo: Photograph) -> int:
        """Insert a row (status defaults to ``pending``) and return its id."""
        cur = self._conn.execute(
            """INSERT INTO photographs
                   (person_id, title, photographer, year, source, source_url, licence,
                    attribution, remote_url, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                photo.person_id,
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

    # ── queries for list / stats / export / verify ─────────────────
    def iter_photographs(
        self,
        *,
        status: str | None = "complete",
        source: str | None = None,
        licences: list[str] | None = None,
        person: str | None = None,
    ) -> list[Photograph]:
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
        if person:
            clauses.append("pe.name = ?")
            params.append(person)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        q = (
            "SELECT ph.*, pe.name AS person_name FROM photographs ph "
            "JOIN people pe ON pe.id = ph.person_id" + where + " ORDER BY pe.name, ph.id"
        )
        return [self._row_to_photo(r) for r in self._conn.execute(q, params) if r is not None]

    def counts_by(self, column: str, *, only_complete: bool = True) -> dict[str, int]:
        if column not in {"source", "licence", "status"}:
            raise ValueError(f"unsupported group column: {column}")
        q = f"SELECT {column} AS k, COUNT(*) AS n FROM photographs"
        if only_complete:
            q += " WHERE status = 'complete'"
        q += f" GROUP BY {column}"
        return {(r["k"] or "?"): int(r["n"]) for r in self._conn.execute(q)}

    def stats(self) -> dict[str, object]:
        total = int(self._conn.execute("SELECT COUNT(*) AS n FROM photographs").fetchone()["n"])
        people = int(self._conn.execute("SELECT COUNT(*) AS n FROM people").fetchone()["n"])
        return {
            "people": people,
            "photographs": total,
            "by_status": self.counts_by("status", only_complete=False),
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
            person_id=row["person_id"],
            person_name=row["person_name"] if "person_name" in keys else "",
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
