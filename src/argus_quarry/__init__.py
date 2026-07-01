"""argus-quarry — the acquisition stage of the Argus suite.

Digs up raw material: acquires public-domain / CC0 portrait images from upstream
archives and lands them — with full provenance and licensing — into a folder the
rest of the suite consumes (``DATASET_DIR`` -> ``/data/images``). Deliberately
lean: acquisition + provenance only. Quality scoring, near-dup, faces and
captioning are owned downstream by argus-curator and argus-lens.
"""

from __future__ import annotations

__version__ = "0.1.0"

from argus_quarry.config import QuarryConfig, SourceRate
from argus_quarry.models import (
    Person,
    Photograph,
    PortraitRecord,
    is_accepted_licence,
    normalise_licence,
    slugify_name,
)

__all__ = [
    "__version__",
    "QuarryConfig",
    "SourceRate",
    "Person",
    "Photograph",
    "PortraitRecord",
    "is_accepted_licence",
    "normalise_licence",
    "slugify_name",
]
