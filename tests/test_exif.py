"""EXIF text: non-ASCII attribution and NUL-terminated ASCII tags."""

from __future__ import annotations

from argus_quarry.exif import exif_text


def test_exif_text_keeps_non_ascii():
    assert exif_text("Jöhn Müller".encode()) == "Jöhn Müller"
    assert exif_text("©".encode()) == "©"
    assert exif_text(b"\xe9cole") == "école"  # Latin-1, not valid UTF-8


def test_exif_text_drops_nul_padding():
    assert exif_text("abc\x00 \x00") == "abc"
    assert exif_text(b"Canon\x00\x00 ") == "Canon"
    assert exif_text("  Nikon \x00 trailing") == "Nikon"
