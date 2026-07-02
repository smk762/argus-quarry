"""argus-quarry — the acquisition stage of the Argus suite.

Digs up raw material: acquires public-domain / CC0 portrait images from upstream
archives and lands them — with full provenance and licensing — into a folder the
rest of the suite consumes (``DATASET_DIR`` -> ``/data/images``). Deliberately
lean: acquisition + provenance only. Quality scoring, near-dup, faces and
captioning are owned downstream by argus-curator and argus-lens.
"""

from __future__ import annotations

try:
    # Written by hatch-vcs at build time (see pyproject [tool.hatch.build.hooks.vcs]).
    from argus_quarry._version import __version__
except ImportError:  # running from a source checkout that hasn't been built
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("argus-quarry")
    except PackageNotFoundError:
        __version__ = "0.0.0+unknown"

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
