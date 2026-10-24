"""Megalith-10m — 9.58M Flickr photos marked CC0, Public Domain Mark, Flickr Commons or US Gov.

Archived layout (``QUARRY_MEGALITH_DIR``), as fetched by
``scripts/fetch_public_domain_metadata.py``:

    links/data/*.parquet     url (500px), url_highres (1024px), url_source (photo page)
    captions/metadata.csv    url_source, caption (Florence-2; 8.94M rows)
    drawthings/*.parquet     url (= url_highres), status, width, height, sha256
                             for the ~9.4M images drawthingsai archived

The link list is shuffled; the only built-in grouping is the owner account in
``url_source``, which becomes the ``collection`` facet. Megalith carries no
per-photo licence column, only the dataset-level filter, so every record states
that filter as its licence label rather than inventing a per-photo one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from argus_quarry.archives.base import Archive, sql_path
from argus_quarry.models import SourceRecord

DATASET_URL = "https://huggingface.co/datasets/madebyollin/megalith-10m"
LICENCE = "public domain or equivalent (Megalith-10m filter: Flickr PDM / CC-zero / Flickr Commons / US Gov)"


class MegalithArchive(Archive):
    name = "megalith"
    source = "megalith"
    default_medium = "photo"  # Megalith's builders filtered out non-photos with CLIP
    title = "Megalith-10m"
    description = (
        "9.58M Flickr photos marked public domain, CC0, Flickr Commons or US Gov; shuffled, "
        "with Florence-2 captions and 1024px sizes for the ~9.4M Drawthings archived."
    )

    def _stage_sql(self, root: Path) -> list[str]:
        captions = root / "captions" / "metadata.csv"
        drawthings = root / "drawthings"
        stage = []
        if captions.is_file():
            # parallel = false: every caption is a quoted multi-line field, and DuckDB's
            # parallel reader mis-splits rows when a chunk boundary lands inside one (it
            # failed at line 143,592 of the real file, and dropped ~12k rows with
            # ignore_errors). Serial reads all 9,148,514 rows. url_source is unique in the
            # published file, so no GROUP BY here (it cost GBs over 9M long strings);
            # build_index warns if a duplicate id ever comes through the join.
            stage.append(f"""
                CREATE TEMP TABLE caps AS
                SELECT url_source, trim(caption, ' ' || chr(9) || chr(10) || chr(13)) AS caption
                FROM read_csv({sql_path(captions)}, header = true, delim = ',', quote = '"', escape = '"',
                              parallel = false, columns = {{'url_source': 'VARCHAR', 'caption': 'VARCHAR'}})
            """)
        else:
            stage.append("CREATE TEMP TABLE caps (url_source VARCHAR, caption VARCHAR)")
        if any(drawthings.glob("*.parquet")):
            stage.append(f"""
                CREATE TEMP TABLE sizes AS
                SELECT url AS url_highres, CAST(width AS INTEGER) AS width, CAST(height AS INTEGER) AS height
                FROM read_parquet({sql_path(drawthings / "*.parquet")})
                WHERE status = 'success'
            """)
        else:
            stage.append("CREATE TEMP TABLE sizes (url_highres VARCHAR, width INTEGER, height INTEGER)")
        return stage

    def _select_sql(self, root: Path) -> str:
        links = sql_path(root / "links" / "data" / "*.parquet")
        return f"""
            SELECT
                regexp_extract(l.url_source, '/photos/[^/]+/([0-9]+)', 1) AS id,
                l.url_highres AS url,
                l.url AS thumb_url,
                l.url_source AS source_url,
                c.caption AS caption,
                d.width AS width,
                d.height AS height,
                'public domain or equivalent' AS licence,
                regexp_extract(l.url_source, '/photos/([^/]+)/', 1) AS collection,
                'image/jpeg' AS mime
            FROM read_parquet({links}) l
            LEFT JOIN caps c ON c.url_source = l.url_source
            LEFT JOIN sizes d ON d.url_highres = l.url_highres
        """

    def to_record(self, item: dict[str, Any], *, category: str, subject: str) -> SourceRecord:
        return SourceRecord(
            subject=subject,
            category=category,
            title=(item.get("caption") or "")[:200] or None,
            photographer=item.get("collection"),
            remote_url=item["url"],
            source=self.source,
            source_url=item["source_url"],
            licence=LICENCE,
            attribution=f"Flickr user {item['collection']} via Megalith-10m ({DATASET_URL})",
        )
