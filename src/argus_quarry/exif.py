"""The capture device a landed file's EXIF names, and what that device says about how it was captured.

EXIF ``Make`` (271) / ``Model`` (272) name the device that wrote the file. In #55 that device was the
best evidence of the capture medium: a film scanner means a scan of film or a print, a camera or a
phone means a digital capture. Both read the device, not the subject: a digital camera photographing
a print reads as ``digital``. A file that names no device is unknown, never ``digital``.
"""

from __future__ import annotations

import re

from PIL import Image

#: EXIF IFD0 tags naming the device that made the file.
_MAKE, _MODEL = 271, 272
#: Devices whose EXIF marks a scan of film or a print. Every other camera or phone model is a digital capture.
SCANNER = re.compile(r"scan|coolscan|perfection|noritsu|ez controller|pentacon", re.IGNORECASE)
#: Values of ``capture_medium``.
DIGITAL, SCAN = "digital", "scan"


def exif_text(value: object) -> str:
    """An EXIF string value as clean text.

    ASCII tags often carry UTF-8 (or, failing that, Latin-1). The value is NUL-terminated, so
    everything after the first NUL is padding and is discarded before the surrounding whitespace.
    """
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            value = value.decode("latin-1")
    return str(value).split("\x00", 1)[0].strip()


def capture_medium(make: str | None, model: str | None) -> str | None:
    """``scan`` for a film/flatbed scanner, ``digital`` for a camera or phone, None when no model is named."""
    if not (model or "").strip():
        return None
    return SCAN if SCANNER.search(f"{make or ''} {model}") else DIGITAL


def exif_device(image: Image.Image) -> tuple[str | None, str | None]:
    """``(make, model)`` from the image's EXIF IFD0, each None when blank or absent.

    Metadata is opportunistic: a malformed EXIF block reads as no device, never an error.
    """
    try:
        exif = image.getexif()
        make, model = (exif_text(exif.get(tag) or "") for tag in (_MAKE, _MODEL))
    except Exception:
        return None, None
    return make or None, model or None
