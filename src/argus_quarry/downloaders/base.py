"""The downloader contract.

Every source module subclasses :class:`Downloader` and yields source-independent
:class:`~argus_quarry.models.SourceRecord` objects from :meth:`harvest`. The
downloader is responsible only for *discovering candidates + their provenance*;
ingest (download/verify/cap/dedup/land) is shared and lives in
:mod:`argus_quarry.ingest`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from argus_quarry.config import QuarryConfig
from argus_quarry.models import SourceRecord, Subject
from argus_quarry.net import NetClient


class Downloader(ABC):
    """Base class for a single archive/source."""

    #: Stable source key stored in the DB (e.g. ``"commons"``).
    name: str = "base"

    def __init__(self, config: QuarryConfig, net: NetClient) -> None:
        self.config = config
        self.net = net

    @abstractmethod
    def harvest(self, subject: Subject, limit: int) -> Iterator[SourceRecord]:
        """Yield up to ``limit`` candidate records for ``subject``.

        Search with ``subject.query`` and stamp every record with the subject's
        canonical folder name and category. Implementations must attach full
        provenance (source_url, licence, attribution) to every record. Records
        whose licence is not clearly free-to-use should still be yielded with
        their raw licence string — ingest quarantines them centrally so the
        policy lives in one place.
        """
        raise NotImplementedError
