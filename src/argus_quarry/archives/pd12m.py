"""PD12M (Spawning) — 12.4M CC0 / Public Domain Mark images from 640 institutions.

Archived layout (``QUARRY_PD12M_DIR``): ``metadata/**/*.parquet`` with ``id, url,
caption, width, height, mime_type, hash, license, source``. Images live in the
public ``s3://pd12m`` bucket the ``url`` column points at. PD12M records no
per-image source page, so provenance is the S3 object plus the contributing
institution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from argus_quarry.archives.base import Archive, sql_path
from argus_quarry.models import SourceRecord

DATASET_URL = "https://huggingface.co/datasets/Spawning/PD12M"


class PD12MArchive(Archive):
    name = "pd12m"
    source = "pd12m"
    title = "PD12M"
    description = (
        "12.4M CC0 / Public Domain Mark images from 640 institutions (Wikimedia Commons, iNaturalist, "
        "Rijksmuseum, the Met, …), recaptioned and safety-filtered by Spawning."
    )

    def _select_sql(self, root: Path) -> str:
        # Recursive: a Hugging Face mirror of the repo nests it as metadata/metadata/*.parquet.
        files = sql_path(root / "metadata" / "**" / "*.parquet")
        return f"""
            SELECT
                id,
                url,
                CAST(NULL AS VARCHAR) AS thumb_url,
                url AS source_url,
                caption,
                CAST(width AS INTEGER) AS width,
                CAST(height AS INTEGER) AS height,
                CASE WHEN license LIKE '%publicdomain/zero%' THEN 'CC0'
                     WHEN license LIKE '%publicdomain/mark%' THEN 'PDM'
                     ELSE license END AS licence,
                source AS collection,
                mime_type AS mime
            FROM read_parquet({files})
        """

    def to_record(self, item: dict[str, Any], *, category: str, subject: str) -> SourceRecord:
        # "CC0" / "PDM" (Public Domain Mark) both pass the ingest licence-gate.
        licence = {"PDM": "Public Domain Mark 1.0", "CC0": "CC0 1.0"}.get(item["licence"], item["licence"])
        return SourceRecord(
            subject=subject,
            category=category,
            title=(item.get("caption") or "")[:200] or None,
            remote_url=item["url"],
            source=self.source,
            source_url=item["source_url"] or item["url"],
            licence=licence,
            attribution=f"{item['collection']} via PD12M ({DATASET_URL})" if item.get("collection") else DATASET_URL,
        )
