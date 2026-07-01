"""People sources — the subject list downloaders harvest around.

Phase 1 ships the curated, deterministic seed (``seeds/people.yaml``). The
Wikidata SPARQL harvester (``--from-wikidata``) lands in Phase 2; both resolve
to the same :class:`~argus_quarry.models.Person` shape so downloaders never care
which was used.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml

from argus_quarry.models import Person

SEED_RESOURCE = "people.yaml"


def _parse_people(data: object) -> list[Person]:
    if not isinstance(data, dict) or "people" not in data:
        raise ValueError("seed file must be a mapping with a top-level 'people' list")
    people = []
    for entry in data["people"]:
        people.append(Person(**entry))
    return people


def load_seed(path: str | Path | None = None) -> list[Person]:
    """Load the curated people seed.

    With no ``path`` the packaged ``seeds/people.yaml`` is used; pass a path to
    override with a local list.
    """
    if path is not None:
        text = Path(path).read_text(encoding="utf-8")
    else:
        text = resources.files("argus_quarry.seeds").joinpath(SEED_RESOURCE).read_text(encoding="utf-8")
    return _parse_people(yaml.safe_load(text))


def load_people(
    *, from_wikidata: bool = False, seed_path: str | Path | None = None, limit: int | None = None
) -> list[Person]:
    """Resolve the people list from the chosen source.

    ``from_wikidata`` is reserved for the Phase 2 SPARQL harvester and currently
    raises ``NotImplementedError`` rather than silently falling back.
    """
    if from_wikidata:
        raise NotImplementedError("Wikidata SPARQL harvester lands in Phase 2 (--from-wikidata)")
    people = load_seed(seed_path)
    if limit is not None:
        people = people[:limit]
    return people
