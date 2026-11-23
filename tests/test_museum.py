"""Museum catalogue dates for PD12M (#52): exact matching on PD12M's uuid5 filenames, the Met first."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

duckdb = pytest.importorskip("duckdb", reason="archives extra not installed")

from argus_quarry.archives import ArchiveQuery, get_archive  # noqa: E402
from argus_quarry.archives import museum as mu  # noqa: E402
from argus_quarry.config import QuarryConfig, SourceRate  # noqa: E402
from argus_quarry.net import NetClient  # noqa: E402

S3 = "https://pd12m.s3.us-west-2.amazonaws.com/images/{}.jpeg"
#: Tests don't wait on the real pacing.
FAST = {mu.MET_RATE_SOURCE: SourceRate(rate_per_sec=10_000, burst=10_000)}


def test_pd12m_names_each_copy_uuid5_of_the_museum_image_url():
    """A real pair: Met object 4706's primaryImage and the PD12M row that holds its copy."""
    url = "https://images.metmuseum.org/CRDImages/ad/original/DP239096.jpg"
    assert mu.image_uuid(url) == "92889c07-87c0-5e18-b16c-af75cb6b04a8"


def _met(object_id: int, **kw) -> dict:
    base = {
        "objectID": object_id,
        "isPublicDomain": True,
        "primaryImage": f"https://images.metmuseum.org/CRDImages/x/original/{object_id}.jpg",
        "additionalImages": [],
        "objectDate": "ca. 1815",
        "objectBeginDate": 1812,
        "objectEndDate": 1815,
        "objectURL": f"https://www.metmuseum.org/art/collection/search/{object_id}",
    }
    return base | kw


def test_parsing_keeps_every_image_and_reads_0_0_with_no_text_as_undated():
    obj = mu.parse_met_object(_met(1, additionalImages=["https://images.metmuseum.org/a.jpg"]))
    assert obj.images == (
        "https://images.metmuseum.org/CRDImages/x/original/1.jpg",
        "https://images.metmuseum.org/a.jpg",
    )
    assert (obj.date, obj.year_begin, obj.year_end) == ("ca. 1815", 1812, 1815)
    undated = mu.parse_met_object(_met(2, objectDate="", objectBeginDate=0, objectEndDate=0))
    assert (undated.date, undated.year_begin, undated.year_end) == (None, None, None)
    bce = mu.parse_met_object(_met(3, objectDate="5th century B.C.", objectBeginDate=-499, objectEndDate=-400))
    assert (bce.year_begin, bce.year_end) == (-499, -400)  # BCE stays negative, never dropped
    swapped = mu.parse_met_object(_met(4, objectBeginDate=1900, objectEndDate=1850))
    assert (swapped.year_begin, swapped.year_end) == (1850, 1900)
    with pytest.raises(mu.MuseumUnavailable):
        mu.parse_met_object({"message": "not an object"})


class MetServer:
    """A Met API stand-in: ``objects`` by id, 404 for the rest, optionally failing some ids for good."""

    def __init__(self, objects: dict[int, dict], *, failing: frozenset[int] = frozenset()) -> None:
        self.objects, self.failing = objects, failing
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request.url.path)
        tail = request.url.path.rsplit("/", 1)[1]
        if tail == "objects":
            return httpx.Response(200, json={"total": 5, "objectIDs": [*self.objects, 404, *self.failing]})
        oid = int(tail)
        if oid in self.failing:
            return httpx.Response(503)
        if oid in self.objects:
            return httpx.Response(200, json=self.objects[oid])
        return httpx.Response(404, json={"message": "ObjectID not found"})


def _client(tmp_path: Path, server: MetServer) -> mu.MetClient:
    cfg = QuarryConfig(home=tmp_path / "q", backoff_base=0.0, max_retries=2, rates=FAST)
    return mu.MetClient(NetClient(cfg, client=httpx.Client(transport=httpx.MockTransport(server))))


def test_the_harvest_resumes_caches_gone_objects_and_retries_failures(tmp_path):
    server = MetServer({1: _met(1), 2: _met(2)}, failing=frozenset({7}))
    client, index = _client(tmp_path, server), tmp_path / "idx"
    ids = mu.object_ids(client, index, "pd12m")
    assert ids == [1, 2, 7, 404]
    first = mu.harvest(client, index, "pd12m", ids, limit=2)
    assert first == {"cached": 0, "asked": 2, "gone": 0, "failed": 0, "blocked": 0}
    again = mu.harvest(client, index, "pd12m", mu.object_ids(client, index, "pd12m"))  # id list read from the cache
    assert again == {"cached": 2, "asked": 1, "gone": 1, "failed": 1, "blocked": 0}  # 404 cached as gone, 7 asked again
    assert server.asked.count("/public/collection/v1/objects") == 1
    cache = mu.read_cache(mu.cache_path(index, "pd12m"))
    assert set(cache) == {1, 2, 404} and cache[404] == {"id": 404, "gone": True}
    assert 7 not in cache


@pytest.mark.parametrize(
    "refusal",
    [httpx.Response(403, text="blocked"), httpx.Response(410), httpx.Response(200, text="<html>challenge</html>")],
    ids=["403", "410", "challenge-page"],
)
def test_a_firewall_block_stops_the_harvest_and_is_never_cached_as_gone(tmp_path, refusal):
    """The Met's firewall answered a 10/s run with refusals: recording those as 'gone' poisoned the cache."""
    objects = {1: _met(1)}

    def handler(request: httpx.Request) -> httpx.Response:
        tail = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=objects[tail]) if tail in objects else refusal

    cfg = QuarryConfig(home=tmp_path / "q", backoff_base=0.0, max_retries=2, rates=FAST)
    client = mu.MetClient(NetClient(cfg, client=httpx.Client(transport=httpx.MockTransport(handler))))
    index = tmp_path / "idx"
    counts = mu.harvest(client, index, "pd12m", [1, 2, 3, 4])
    assert counts["blocked"] == 1 and counts["asked"] == 1  # stopped at the first refusal
    assert set(mu.read_cache(mu.cache_path(index, "pd12m"))) == {1}  # nothing recorded for 2, 3, 4


def test_a_patient_harvest_waits_a_block_out_and_finishes(tmp_path):
    """The Met lets an IP back in after a pause: the harvest sleeps, resumes from the cache, and completes."""
    blocked = {"left": 3}  # three refusals, then the firewall lets go

    def handler(request: httpx.Request) -> httpx.Response:
        if blocked["left"] and request.url.path.endswith("/3"):
            blocked["left"] -= 1
            return httpx.Response(403)
        return httpx.Response(200, json=_met(int(request.url.path.rsplit("/", 1)[1])))

    cfg = QuarryConfig(home=tmp_path / "q", backoff_base=0.0, max_retries=2, rates=FAST)
    client = mu.MetClient(NetClient(cfg, client=httpx.Client(transport=httpx.MockTransport(handler))))
    slept: list[float] = []
    counts = mu.harvest_patiently(client, tmp_path / "idx", "pd12m", [1, 2, 3, 4], block_wait=7, sleep=slept.append)
    assert counts == {"cached": 0, "asked": 4, "gone": 0, "failed": 0, "blocked": 0, "blocks": 3}
    assert slept == [7, 7, 7]
    assert set(mu.read_cache(mu.cache_path(tmp_path / "idx", "pd12m"))) == {1, 2, 3, 4}
    # block_wait 0: stop at the first block; max_blocks: give up on a firewall that never lets go
    blocked["left"] = 99
    stop = mu.harvest_patiently(client, tmp_path / "idx2", "pd12m", [3], block_wait=0, sleep=slept.append)
    assert stop["blocked"] == 1 and stop["blocks"] == 1
    give_up = mu.harvest_patiently(
        client, tmp_path / "idx3", "pd12m", [3], block_wait=1, max_blocks=4, sleep=slept.append
    )
    assert give_up["blocked"] == 1 and give_up["blocks"] == 4


def test_a_run_of_failures_stops_the_harvest(tmp_path):
    server = MetServer({}, failing=frozenset(range(1, 100)))
    counts = mu.harvest(_client(tmp_path, server), tmp_path / "idx", "pd12m", list(range(1, 100)))
    assert counts["blocked"] == 1 and counts["failed"] == mu.MAX_CONSECUTIVE_FAILURES


def test_a_line_cut_short_by_an_interrupted_write_is_asked_again(tmp_path):
    index = tmp_path / "idx"
    index.mkdir()
    mu.cache_path(index, "pd12m").write_text(json.dumps(mu.parse_met_object(_met(1)).record()) + '\n{"id": 2, "pd": tr')
    assert set(mu.read_cache(mu.cache_path(index, "pd12m"))) == {1}


def _harvested(index: Path, objects: list[dict]) -> None:
    index.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(mu.parse_met_object(o).record()) for o in objects] + [json.dumps({"id": 99, "gone": True})]
    mu.cache_path(index, "pd12m").write_text("\n".join(lines) + "\n")


def test_compile_keeps_public_domain_images_once_each(tmp_path):
    shared = "https://images.metmuseum.org/shared.jpg"
    index = tmp_path / "idx"
    _harvested(
        index,
        [
            _met(5, additionalImages=[shared]),
            _met(3, additionalImages=[shared], objectDate="1900", objectBeginDate=1900, objectEndDate=1900),
            _met(8, isPublicDomain=False),  # in copyright: never matched
        ],
    )
    assert mu.compile_table(index, "pd12m") == {"images": 3, "objects": 2}
    rows = duckdb.connect().sql(
        f"SELECT image_uuid, museum_object_id FROM read_parquet('{mu.museum_path(index, 'pd12m')}')"
    )
    owner = dict(rows.fetchall())
    assert owner[mu.image_uuid(shared)] == "3"  # an image two objects share goes to the lower id
    assert not [p for p in index.iterdir() if p.name.endswith(".tmp")]


def test_compile_keeps_the_lower_objects_null_fields(tmp_path):
    """A null date on the winning object stays null; it is not filled from a higher id."""
    shared = "https://images.metmuseum.org/shared.jpg"
    index = tmp_path / "idx"
    _harvested(
        index,
        [
            _met(1, additionalImages=[shared], objectDate="", objectBeginDate=0, objectEndDate=0, objectURL=""),
            _met(2, additionalImages=[shared], objectDate="1900", objectBeginDate=1900, objectEndDate=1900),
        ],
    )
    mu.compile_table(index, "pd12m")
    row = (
        duckdb.connect()
        .execute(
            "SELECT museum_object_id, museum_date, museum_year_begin, museum_object_url "
            "FROM read_parquet(?) WHERE image_uuid = ?",
            [str(mu.museum_path(index, "pd12m")), mu.image_uuid(shared)],
        )
        .fetchone()
    )
    assert row == ("1", None, None, None)


def test_compile_quotes_a_path_that_contains_a_quote(tmp_path):
    index = tmp_path / "o'brien"
    _harvested(index, [_met(1)])
    try:
        result = mu.compile_table(index, "pd12m")
    except Exception as exc:
        pytest.fail(f"compile_table rejected a path containing a quote: {exc}")
    assert result["images"] == 1


def _pd12m(tmp_path: Path, urls: list[str]) -> Path:
    """A PD12M archive (one parquet, no embeddings) whose S3 copies are named as PD12M names them."""
    root = tmp_path / "pd12m"
    out = root / "metadata" / "part-0.parquet"
    out.parent.mkdir(parents=True)
    rows = [
        (f"id{i}", S3.format(mu.image_uuid(u)), f"caption {i}", 100, 100, "image/jpeg", "h", "x", mu.MET_COLLECTION)
        for i, u in enumerate(urls)
    ] + [("other", S3.format("00000000-0000-5000-8000-000000000000"), "c", 1, 1, "image/jpeg", "h", "x", "Rijksmuseum")]
    values = ", ".join("(?, ?, ?, ?, ?, ?, ?, ?, ?)" for _ in rows)
    duckdb.connect().execute(
        f"COPY (SELECT * FROM (VALUES {values}) t(id, url, caption, width, height, mime_type, hash, license, source)) "
        f"TO '{out}' (FORMAT parquet)",
        [v for r in rows for v in r],
    )
    return root


def test_matched_pd12m_items_carry_the_museum_date_and_object(tmp_path):
    objs = [
        _met(10),  # 1812-1815
        _met(11, objectDate="1920s", objectBeginDate=1920, objectEndDate=1929),
        _met(12, objectDate="1800-1900", objectBeginDate=1800, objectEndDate=1900),
    ]
    urls = [o["primaryImage"] for o in objs] + ["https://images.metmuseum.org/never-harvested.jpg"]
    cfg = QuarryConfig(home=tmp_path / "q", archive_dirs={"pd12m": _pd12m(tmp_path, urls)})
    archive = get_archive("pd12m", cfg)
    archive.build_index()
    assert archive.facets()["museum_matched"] == 0  # nothing harvested yet: no museum columns filled

    _harvested(cfg.archive_index_dir, objs)
    mu.compile_table(cfg.archive_index_dir, "pd12m")
    archive.build_index()
    items = {i["id"]: i for i in archive.search(ArchiveQuery(), limit=10)["items"]}
    assert items["id0"]["museum"] == "met" and items["id0"]["museum_object_id"] == "10"
    assert items["id0"]["museum_object_url"].endswith("/10") and items["id0"]["museum_date"] == "ca. 1815"
    assert items["id3"]["museum_object_id"] is None and items["other"]["museum"] is None  # unmatched: no guess

    def ids(**q) -> set[str]:
        return {i["id"] for i in archive.search(ArchiveQuery(**q), limit=10)["items"]}

    assert ids(museum_year_min=1900) == {"id1"}
    assert ids(museum_year_max=1850) == {"id0"}
    assert ids(museum_year_min=1890, museum_year_max=1950) == {"id1"}  # 1800-1900 isn't wholly inside
    facets = archive.facets()
    assert facets["museum_matched"] == 3 and facets["museum_dated"] == 3 and facets["museum_year_range"] == [1800, 1929]
    assert facets["date_sources"]["museum_year"] == "museum-catalogue"
    status = archive.status()["museum"]
    assert status["compiled"] and status["images"] == 3 and status["museums"] == ["met"]


def test_megalith_has_no_museum():
    from argus_quarry.archives.megalith import MegalithArchive

    assert MegalithArchive.museums == ()
