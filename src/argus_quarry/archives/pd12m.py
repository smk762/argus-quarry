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

from argus_quarry.archives import museum as mu
from argus_quarry.archives.base import Archive, sql_path
from argus_quarry.models import SourceRecord

DATASET_URL = "https://huggingface.co/datasets/Spawning/PD12M"


class PD12MArchive(Archive):
    name = "pd12m"
    source = "pd12m"
    title = "PD12M"
    embedding_model = "openai/clip-vit-large-patch14"  # CLIP ViT-L/14, matched by the text tower
    museums = (mu.MET,)
    description = (
        "12.4M CC0 / Public Domain Mark images from 640 institutions (Wikimedia Commons, iNaturalist, "
        "Rijksmuseum, the Met, …), recaptioned and safety-filtered by Spawning."
    )

    def _select_sql(self, root: Path) -> str:
        # Recursive: a Hugging Face mirror of the repo nests it as metadata/metadata/*.parquet.
        files = sql_path(root / "metadata" / "**" / "*.parquet")
        # pos: the row in the published embeddings, which are row-aligned with the
        # metadata files concatenated in sorted-filename order (checked on the real
        # archive: caption/image CLIP cosine 0.27 aligned vs 0.09 one row off). Each
        # file's start row comes from the parquet footers, so this costs no extra scan.
        # Museum dates (#52) join on the S3 filename, which PD12M derived as uuid5(URL, museum image URL).
        table = mu.museum_path(self.config.archive_index_dir, self.name)
        museum_cols, museum_join = mu.NO_MUSEUM_SQL, ""
        if table.is_file():
            museum_cols = ", ".join(f"m.{c}" for c in mu.COLUMNS)
            museum_join = (
                f" LEFT JOIN ({mu.join_sql(table)}) m"
                f" ON m.image_uuid = regexp_extract(p.url, '/images/([0-9a-f-]{{36}})[.]', 1)"
            )
        return f"""
            WITH files AS (
                SELECT file_name,
                       coalesce(sum(num_rows) OVER (ORDER BY file_name
                                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS start
                FROM parquet_file_metadata({files})
            )
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
                mime_type AS mime,
                f.start + p.file_row_number AS pos,
                CAST(NULL AS INTEGER) AS upload_year,
                {museum_cols}
            FROM read_parquet({files}, filename = true, file_row_number = true) p
            JOIN files f ON f.file_name = p.filename{museum_join}
        """

    def _embedding_files(self, root: Path) -> list[Path]:
        base = root / "embeddings"
        # Skip hidden dirs: the Hugging Face download cache (.cache/) sits alongside.
        found = (p for p in base.rglob("*.npy") if not any(part.startswith(".") for part in p.relative_to(base).parts))
        return sorted(found, key=str)

    def to_record(self, item: dict[str, Any], *, category: str, subject: str) -> SourceRecord:
        # "CC0" / "PDM" (Public Domain Mark) both pass the ingest licence-gate. A row with
        # no licence still becomes a record, so ingest quarantines it instead of the
        # whole slice failing validation.
        licence = item["licence"] or "unknown"
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
