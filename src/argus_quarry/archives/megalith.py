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
    title = "Megalith-10m"
    description = (
        "9.58M Flickr photos marked public domain, CC0, Flickr Commons or US Gov; shuffled, "
        "with Florence-2 captions and 1024px sizes for the ~9.4M Drawthings archived."
    )

    def _select_sql(self, root: Path) -> str:
        links = sql_path(root / "links" / "data" / "*.parquet")
        captions = root / "captions" / "metadata.csv"
        drawthings = root / "drawthings"
        caption_join = (
            f"""LEFT JOIN (
                    SELECT url_source, any_value(trim(caption, ' ' || chr(9) || chr(10) || chr(13))) AS caption
                    FROM read_csv({sql_path(captions)}, header = true,
                                  columns = {{'url_source': 'VARCHAR', 'caption': 'VARCHAR'}})
                    GROUP BY url_source
                ) c USING (url_source)"""
            if captions.is_file()
            else "LEFT JOIN (SELECT NULL::VARCHAR AS url_source, NULL::VARCHAR AS caption) c USING (url_source)"
        )
        size_join = (
            f"""LEFT JOIN (
                    SELECT url AS url_highres, any_value(width) AS width, any_value(height) AS height
                    FROM read_parquet({sql_path(drawthings / "*.parquet")})
                    WHERE status = 'success'
                    GROUP BY url
                ) d USING (url_highres)"""
            if any(drawthings.glob("*.parquet"))
            else "LEFT JOIN (SELECT NULL::VARCHAR AS url_highres, NULL::INTEGER AS width, "
            "NULL::INTEGER AS height) d USING (url_highres)"
        )
        return f"""
            SELECT
                regexp_extract(url_source, '/photos/[^/]+/([0-9]+)', 1) AS id,
                url_highres AS url,
                l.url AS thumb_url,
                url_source AS source_url,
                c.caption AS caption,
                CAST(d.width AS INTEGER) AS width,
                CAST(d.height AS INTEGER) AS height,
                'public domain or equivalent' AS licence,
                regexp_extract(url_source, '/photos/([^/]+)/', 1) AS collection,
                'image/jpeg' AS mime
            FROM read_parquet({links}) l
            {caption_join}
            {size_join}
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
