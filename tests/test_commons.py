from __future__ import annotations

from conftest import FakeNet

from argus_quarry.downloaders.commons import API_URL, CommonsDownloader
from argus_quarry.models import Subject


def _canned_response() -> dict:
    return {
        "query": {
            "pages": {
                "1": {
                    "title": "File:Einstein_1921.jpg",
                    "imageinfo": [
                        {
                            "url": "https://upload.wikimedia.org/einstein_1921.jpg",
                            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Einstein_1921.jpg",
                            "mime": "image/jpeg",
                            "width": 800,
                            "height": 600,
                            "size": 54321,
                            "extmetadata": {
                                "LicenseShortName": {"value": "Public domain"},
                                "Artist": {"value": '<a href="x">Ferdinand Schmutzer</a>'},
                                "DateTimeOriginal": {"value": "1921"},
                            },
                        }
                    ],
                },
                "2": {  # non-image (e.g. an SVG/PDF) -> skipped
                    "title": "File:Notes.pdf",
                    "imageinfo": [{"url": "https://x/notes.pdf", "mime": "application/pdf"}],
                },
            }
        }
    }


def test_commons_parses_imageinfo(config):
    net = FakeNet(json_by_url={API_URL: _canned_response()})
    dl = CommonsDownloader(config, net)
    records = list(dl.harvest(Subject(name="Albert Einstein", wikidata_id="Q937"), limit=10))

    assert len(records) == 1
    r = records[0]
    assert r.remote_url == "https://upload.wikimedia.org/einstein_1921.jpg"
    assert r.source == "commons"
    assert r.source_url.endswith("File:Einstein_1921.jpg")
    assert r.licence == "Public domain"
    assert r.photographer == "Ferdinand Schmutzer"  # HTML stripped
    assert r.year == 1921
    assert r.subject == "Albert_Einstein"
    assert r.category == "identity"
    assert r.wikidata_id == "Q937"


def test_commons_non_identity_subject_sets_category(config):
    net = FakeNet(json_by_url={API_URL: _canned_response()})
    dl = CommonsDownloader(config, net)
    records = list(dl.harvest(Subject(name="Red dress", category="wardrobe"), limit=10))
    assert len(records) == 1
    assert records[0].subject == "Red_dress"
    assert records[0].category == "wardrobe"


def test_commons_empty_response_yields_nothing(config):
    net = FakeNet(json_by_url={API_URL: {}})
    dl = CommonsDownloader(config, net)
    assert list(dl.harvest(Subject(name="Nobody"), limit=5)) == []


class PagedCommons(FakeNet):
    """Serves ``generator=search`` results per query, paged by ``gsroffset`` like the real API."""

    def __init__(self, hits_by_query: dict[str, list[str]]) -> None:
        super().__init__()
        self.hits_by_query = hits_by_query
        self.calls: list[dict] = []

    def get_json(self, url: str, *, params: dict | None = None, source: str = "default") -> dict:
        assert url == API_URL
        params = dict(params or {})
        self.calls.append(params)
        hits = self.hits_by_query.get(params["gsrsearch"], [])
        offset, size = int(params.get("gsroffset", 0)), int(params["gsrlimit"])
        assert size <= 50  # the API's per-request cap
        chunk = hits[offset : offset + size]
        # Page ids are not in rank order; ``index`` is the search rank.
        pages = {
            str(1000 - offset - i): {
                "index": offset + i + 1,
                "title": f"File:{name}.jpg",
                "imageinfo": [{"url": f"https://upload/{name}.jpg", "mime": "image/jpeg", "extmetadata": {}}],
            }
            for i, name in enumerate(chunk)
        }
        out: dict = {"query": {"pages": pages}} if pages else {}
        if offset + size < len(hits):
            out["continue"] = {"gsroffset": offset + size, "continue": "gsroffset||"}
        return out


def _names(records) -> list[str]:
    return [r.remote_url.removeprefix("https://upload/").removesuffix(".jpg") for r in records]


def test_commons_follows_continue_past_one_page(config):
    net = PagedCommons({"Albert Einstein": [f"e{i:03d}" for i in range(130)]})
    records = list(CommonsDownloader(config, net).harvest(Subject(name="Albert Einstein"), limit=120))
    assert _names(records) == [f"e{i:03d}" for i in range(120)]  # in search-rank order
    assert [c.get("gsroffset", 0) for c in net.calls] == [0, 50, 100]


def test_commons_stops_requesting_once_limit_is_met(config):
    net = PagedCommons({"Albert Einstein": [f"e{i:03d}" for i in range(500)]})
    records = list(CommonsDownloader(config, net).harvest(Subject(name="Albert Einstein"), limit=60))
    assert len(records) == 60 and len(net.calls) == 2


def test_commons_small_limit_requests_a_small_page(config):
    net = PagedCommons({"Albert Einstein": [f"e{i:03d}" for i in range(500)]})
    assert len(list(CommonsDownloader(config, net).harvest(Subject(name="Albert Einstein"), limit=5))) == 5
    assert [c["gsrlimit"] for c in net.calls] == [5]


def test_commons_searches_aliases_and_dedups_across_queries(config):
    net = PagedCommons({"Mark Twain": ["t1", "t2", "shared"], "Samuel Clemens": ["shared", "c1", "c2"]})
    subject = Subject(name="Mark Twain", aliases=["Samuel Clemens", "mark twain", " "])
    records = list(CommonsDownloader(config, net).harvest(subject, limit=10))
    assert _names(records) == ["t1", "t2", "shared", "c1", "c2"]
    assert [c["gsrsearch"] for c in net.calls] == ["Mark Twain", "Samuel Clemens"]  # repeat/blank alias dropped
    assert {r.subject for r in records} == {"Mark_Twain"}


def test_commons_aliases_are_not_searched_once_the_limit_is_met(config):
    net = PagedCommons({"Mark Twain": ["t1", "t2", "t3"], "Samuel Clemens": ["c1"]})
    subject = Subject(name="Mark Twain", aliases=["Samuel Clemens"])
    assert _names(CommonsDownloader(config, net).harvest(subject, limit=3)) == ["t1", "t2", "t3"]
    assert [c["gsrsearch"] for c in net.calls] == ["Mark Twain"]


def test_commons_stuck_continue_token_does_not_loop(config):
    class Stuck(FakeNet):
        calls = 0

        def get_json(self, url, *, params=None, source="default"):
            Stuck.calls += 1
            return {**_canned_response(), "continue": {"gsroffset": 50, "continue": "gsroffset||"}}

    records = list(CommonsDownloader(config, Stuck()).harvest(Subject(name="Albert Einstein"), limit=100))
    assert len(records) == 1 and Stuck.calls == 2
