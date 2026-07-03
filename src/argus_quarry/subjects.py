"""Subject sources — the per-category lists downloaders harvest around.

Phase 1 ships curated, deterministic seeds — one YAML per category
(``seeds/identity.yaml``, ``seeds/wardrobe.yaml``, ``seeds/setting.yaml``,
``seeds/concept.yaml``). The Wikidata SPARQL harvester (``--from-wikidata``,
identity only) lands in Phase 2; every source resolves to the same
:class:`~argus_quarry.models.Subject` shape so downloaders never care which was
used.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml

from argus_quarry.models import SUBJECT_CATEGORIES, Subject, normalise_category


def _parse_subjects(data: object, *, fallback_category: str) -> list[Subject]:
    if not isinstance(data, dict) or "subjects" not in data:
        raise ValueError("seed file must be a mapping with a top-level 'subjects' list")
    file_category = normalise_category(data.get("category") or fallback_category)
    subjects: list[Subject] = []
    for entry in data["subjects"]:
        entry = dict(entry)
        entry.setdefault("category", file_category)
        subjects.append(Subject(**entry))
    return subjects


def load_category(category: str, path: str | Path | None = None) -> list[Subject]:
    """Load one category's curated seed.

    With no ``path`` the packaged ``seeds/<category>.yaml`` is used; pass a path
    to override with a local list.
    """
    category = normalise_category(category)
    if path is not None:
        text = Path(path).read_text(encoding="utf-8")
    else:
        text = resources.files("argus_quarry.seeds").joinpath(f"{category}.yaml").read_text(encoding="utf-8")
    return _parse_subjects(yaml.safe_load(text), fallback_category=category)


def load_subjects(
    *,
    category: str | None = None,
    from_wikidata: bool = False,
    seed_path: str | Path | None = None,
    limit: int | None = None,
) -> list[Subject]:
    """Resolve the subject list from the chosen source(s).

    With no ``category`` all packaged category seeds are merged. ``from_wikidata``
    (identity only) is reserved for the Phase 2 SPARQL harvester and currently
    raises ``NotImplementedError`` rather than silently falling back. ``limit``
    caps the total number of subjects returned.
    """
    if from_wikidata:
        raise NotImplementedError("Wikidata SPARQL harvester lands in Phase 2 (--from-wikidata, identity only)")

    if seed_path is not None:
        subjects = load_category(category or "identity", seed_path)
    elif category is not None:
        subjects = load_category(category)
    else:
        subjects = []
        for cat in SUBJECT_CATEGORIES:
            subjects.extend(load_category(cat))

    if limit is not None:
        subjects = subjects[:limit]
    return subjects


# ── Backward-compatible alias (the pre-0.2 people-only API) ─────────────
def load_people(
    *, from_wikidata: bool = False, seed_path: str | Path | None = None, limit: int | None = None
) -> list[Subject]:
    """Deprecated alias for the identity category (kept for the pre-0.2 API)."""
    return load_subjects(category="identity", from_wikidata=from_wikidata, seed_path=seed_path, limit=limit)
