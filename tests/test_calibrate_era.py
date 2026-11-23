"""Era calibration's row reader: a zero embedding must not become NaN."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("calibrate_era", ROOT / "scripts" / "calibrate_era.py")
calibrate_era = importlib.util.module_from_spec(_spec)
sys.modules["calibrate_era"] = calibrate_era
_spec.loader.exec_module(calibrate_era)


def test_zero_norm_rows_stay_finite(tmp_path):
    path = tmp_path / "rows.npy"
    np.save(path, np.array([[0.0, 0.0, 0.0], [3.0, 0.0, 0.0]], dtype=np.float32))
    out = calibrate_era.Rows([path]).take(np.array([0, 1]))
    assert np.isfinite(out).all()
    assert np.allclose(out[0], 0)
    assert np.allclose(out[1], [1, 0, 0])
