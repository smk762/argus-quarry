from __future__ import annotations

from conftest import FakeNet

from argus_quarry.downloaders.commons import API_URL, CommonsDownloader
from argus_quarry.models import Person


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
    records = list(dl.harvest(Person(name="Albert Einstein", wikidata_id="Q937"), limit=10))

    assert len(records) == 1
    r = records[0]
    assert r.remote_url == "https://upload.wikimedia.org/einstein_1921.jpg"
    assert r.source == "commons"
    assert r.source_url.endswith("File:Einstein_1921.jpg")
    assert r.licence == "Public domain"
    assert r.photographer == "Ferdinand Schmutzer"  # HTML stripped
    assert r.year == 1921
    assert r.person_name == "Albert_Einstein"
    assert r.wikidata_id == "Q937"


def test_commons_empty_response_yields_nothing(config):
    net = FakeNet(json_by_url={API_URL: {}})
    dl = CommonsDownloader(config, net)
    assert list(dl.harvest(Person(name="Nobody"), limit=5)) == []
