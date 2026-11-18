"""Tests for the single-scene NDVI analysis endpoint."""
from __future__ import annotations

import hashlib
import threading

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

TRANSFORM = from_origin(500000, 4000000, 10, 10)
CRS = "EPSG:32650"


def _write_tif(
    path,
    data,
    *,
    transform=TRANSFORM,
    crs=CRS,
    nodata=None,
    scale=None,
    offset=None,
):
    """Write a single-band GeoTIFF, optionally with GDAL scale/offset."""
    array = np.asarray(data)
    with rasterio.open(
        str(path),
        "w",
        driver="GTiff",
        width=array.shape[1],
        height=array.shape[0],
        count=1,
        dtype=array.dtype,
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as ds:
        ds.write(array, 1)
        if scale is not None or offset is not None:
            ds.scales = (1.0 if scale is None else scale,)
            ds.offsets = (0.0 if offset is None else offset,)
    return path


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _submit(client, scene_id, revision, b04_path, b08_path, **kwargs):
    return client.post(
        f"/scenes/{scene_id}/revisions",
        data={
            "revision": str(revision),
            "acquisition_time": kwargs.get("acquisition_time", "2026-06-01T10:00:00Z"),
            "west": "116", "south": "39", "east": "117", "north": "40",
            "sha256_b04": _sha(b04_path),
            "sha256_b08": _sha(b08_path),
        },
        files={
            "file_b04": ("b04.tif", b04_path.read_bytes(), "image/tiff"),
            "file_b08": ("b08.tif", b08_path.read_bytes(), "image/tiff"),
        },
    )


def _submit_bytes(client, scene_id, revision, b04_bytes, b08_bytes):
    return client.post(
        f"/scenes/{scene_id}/revisions",
        data={
            "revision": str(revision),
            "acquisition_time": "2026-06-01T10:00:00Z",
            "west": "116", "south": "39", "east": "117", "north": "40",
            "sha256_b04": hashlib.sha256(b04_bytes).hexdigest(),
            "sha256_b08": hashlib.sha256(b08_bytes).hexdigest(),
        },
        files={
            "file_b04": ("b04.tif", b04_bytes, "image/tiff"),
            "file_b08": ("b08.tif", b08_bytes, "image/tiff"),
        },
    )


def test_ndvi_scaled_reflectance_nodata_and_threshold(client, tmp_path):
    red = _write_tif(tmp_path / "red.tif", np.array([
        [1000, 2000, 0, 4000],
        [5000, 6000, 7000, 8000],
    ], dtype="uint16"), nodata=0, scale=0.0001, offset=0.0)
    nir = _write_tif(tmp_path / "nir.tif", np.array([
        [3000, 4000, 0, 5000],
        [5000, 3000, 7000, 8000],
    ], dtype="uint16"), nodata=0, scale=0.0001, offset=0.0)

    assert _submit(client, "S", 1, red, nir).status_code == 201
    r = client.get("/scenes/S/ndvi", params={"threshold": 0.2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scene_id"] == "S"
    assert body["revision"] == 1
    assert body["threshold"] == 0.2
    # position 3 is NoData in both bands and is excluded
    assert body["valid_pixel_count"] == 7
    expected_mean = (0.5 + 1 / 3 + 1 / 9 + 0 - 1 / 3 + 0 + 0) / 7
    assert body["ndvi_mean"] == pytest.approx(expected_mean, rel=1e-9)
    assert body["fraction_below_threshold"] == pytest.approx(5 / 7, rel=1e-9)


def test_ndvi_without_scale_metadata_uses_raw_values(client, tmp_path):
    red = _write_tif(tmp_path / "red.tif", np.array([[100, 200]], dtype="uint16"))
    nir = _write_tif(tmp_path / "nir.tif", np.array([[300, 100]], dtype="uint16"))

    assert _submit(client, "RAW", 1, red, nir).status_code == 201
    body = client.get("/scenes/RAW/ndvi", params={"threshold": 0}).json()
    assert body["valid_pixel_count"] == 2
    assert body["ndvi_mean"] == pytest.approx((0.5 - 1 / 3) / 2)
    assert body["fraction_below_threshold"] == pytest.approx(0.5)


def test_negative_reflectance_zero_denominator_excluded(client, tmp_path):
    red = _write_tif(
        tmp_path / "red.tif",
        np.array([[100, -100, 0, 500]], dtype="int16"),
        scale=0.0001,
    )
    nir = _write_tif(
        tmp_path / "nir.tif",
        np.array([[300, 300, 0, 500]], dtype="int16"),
        scale=0.0001,
    )

    assert _submit(client, "EDGE", 1, red, nir).status_code == 201
    body = client.get("/scenes/EDGE/ndvi", params={"threshold": 0.6}).json()
    # pixel 1: red reflectance -0.01 -> excluded
    # pixel 2: both reflectances 0 -> zero denominator -> excluded
    # pixel 3: red 0.01, nir 0.03 -> NDVI 0.5
    # pixel 4: red 0.05, nir 0.05 -> NDVI 0
    assert body["valid_pixel_count"] == 2
    assert body["ndvi_mean"] == pytest.approx(0.25)
    assert body["fraction_below_threshold"] == pytest.approx(1.0)


def test_no_valid_pixels_returns_null_stats(client, tmp_path):
    red = _write_tif(tmp_path / "red.tif", np.zeros((3, 3), dtype="uint16"), nodata=0)
    nir = _write_tif(tmp_path / "nir.tif", np.full((3, 3), 500, dtype="uint16"), nodata=0)

    assert _submit(client, "EMPTY", 1, red, nir).status_code == 201
    r = client.get("/scenes/EMPTY/ndvi", params={"threshold": 0.1})
    assert r.status_code == 200
    assert r.json()["valid_pixel_count"] == 0
    assert r.json()["ndvi_mean"] is None
    assert r.json()["fraction_below_threshold"] is None


@pytest.mark.parametrize("threshold", [-1.5, 1.5, "nan", "inf"])
def test_threshold_out_of_range_rejected(client, tmp_path, threshold):
    red = _write_tif(tmp_path / "red.tif", np.array([[100]], dtype="uint16"))
    nir = _write_tif(tmp_path / "nir.tif", np.array([[300]], dtype="uint16"))
    assert _submit(client, "T", 1, red, nir).status_code == 201
    r = client.get("/scenes/T/ndvi", params={"threshold": threshold})
    assert r.status_code == 422


@pytest.mark.parametrize("threshold", [-1, 1])
def test_threshold_bounds_accepted(client, tmp_path, threshold):
    red = _write_tif(tmp_path / "red.tif", np.array([[100]], dtype="uint16"))
    nir = _write_tif(tmp_path / "nir.tif", np.array([[300]], dtype="uint16"))
    assert _submit(client, "T", 1, red, nir).status_code == 201
    r = client.get("/scenes/T/ndvi", params={"threshold": threshold})
    assert r.status_code == 200
    assert r.json()["threshold"] == threshold


def test_unknown_scene_is_404(client):
    r = client.get("/scenes/does-not-exist/ndvi", params={"threshold": 0.3})
    assert r.status_code == 404


def test_non_geotiff_rejects_only_analysis(client, tmp_path):
    red = _write_tif(tmp_path / "red.tif", np.array([[100, 200]], dtype="uint16"))
    nir = _write_tif(tmp_path / "nir.tif", np.array([[300, 100]], dtype="uint16"))
    assert _submit(client, "GOOD", 1, red, nir).status_code == 201
    assert _submit_bytes(client, "BAD", 1, b"this is not a tiff", b"neither is this").status_code == 201

    r = client.get("/scenes/BAD/ndvi", params={"threshold": 0.3})
    assert r.status_code == 422
    assert "GeoTIFF" in r.json()["detail"]

    # Existing behavior for the same scene is untouched.
    scenes = client.get("/scenes").json()
    assert {s["scene_id"] for s in scenes} == {"GOOD", "BAD"}
    dl = client.get("/scenes/BAD/revisions/1/bands/B04/download")
    assert dl.status_code == 200
    assert dl.content == b"this is not a tiff"
    assert client.get("/scenes/GOOD/ndvi", params={"threshold": 0.3}).status_code == 200


def test_grid_mismatch_rejected_without_affecting_catalog(client, tmp_path):
    red = _write_tif(tmp_path / "red.tif", np.zeros((2, 2), dtype="uint16"))
    nir = _write_tif(
        tmp_path / "nir.tif",
        np.ones((2, 2), dtype="uint16"),
        transform=from_origin(500000, 4000000, 20, 20),
    )
    assert _submit(client, "GRID", 1, red, nir).status_code == 201

    r = client.get("/scenes/GRID/ndvi", params={"threshold": 0.3})
    assert r.status_code == 422
    assert "same grid" in r.json()["detail"]

    assert client.get("/scenes/GRID/revisions/1").status_code == 200
    assert client.get("/scenes/GRID/revisions/1/bands/B08/download").status_code == 200


def test_analysis_reads_in_bounded_windows(client, tmp_path, monkeypatch):
    height, width = 1100, 1300
    red = _write_tif(tmp_path / "red.tif", np.full((height, width), 100, dtype="uint16"))
    nir = _write_tif(tmp_path / "nir.tif", np.full((height, width), 300, dtype="uint16"))
    assert _submit(client, "BIG", 1, red, nir).status_code == 201

    calls = []
    original_read = rasterio.io.DatasetReader.read

    def recording_read(self, indexes=None, *args, **kwargs):
        window = kwargs.get("window")
        calls.append(
            (int(window.width), int(window.height)) if window is not None else None
        )
        return original_read(self, indexes, *args, **kwargs)

    monkeypatch.setattr(rasterio.io.DatasetReader, "read", recording_read)
    body = client.get("/scenes/BIG/ndvi", params={"threshold": 0.3}).json()

    assert body["valid_pixel_count"] == height * width
    assert calls, "expected windowed reads"
    assert all(call is not None for call in calls), "no full-scene read allowed"
    # Windows follow the file's block layout but stay near the pixel budget.
    assert all(w * h <= 512 * 512 for w, h in calls)
    assert len(calls) > 2 * 2, "a scene must not be read in one or two calls"


def test_submission_allowed_during_analysis_and_stale_result_withheld(
    client, tmp_path, monkeypatch
):
    red1 = _write_tif(tmp_path / "red1.tif", np.full((2, 2), 100, dtype="uint16"))
    nir1 = _write_tif(tmp_path / "nir1.tif", np.full((2, 2), 300, dtype="uint16"))
    red2 = _write_tif(tmp_path / "red2.tif", np.full((2, 2), 400, dtype="uint16"))
    nir2 = _write_tif(tmp_path / "nir2.tif", np.full((2, 2), 600, dtype="uint16"))
    assert _submit(client, "RACE", 1, red1, nir1).status_code == 201

    import app.main as main_module

    real_compute = main_module.compute_ndvi
    analysis_started = threading.Event()
    allow_finish = threading.Event()

    def slow_compute(b04_path, b08_path, threshold):
        stats = real_compute(b04_path, b08_path, threshold)
        analysis_started.set()
        assert allow_finish.wait(timeout=10)
        return stats

    monkeypatch.setattr(main_module, "compute_ndvi", slow_compute)

    result = {}

    def run_analysis():
        result["response"] = client.get("/scenes/RACE/ndvi", params={"threshold": 0.3})

    thread = threading.Thread(target=run_analysis)
    thread.start()
    assert analysis_started.wait(timeout=10)

    # A new revision can be submitted while the analysis is running.
    submitted = _submit(client, "RACE", 2, red2, nir2)
    assert submitted.status_code == 201, submitted.text

    allow_finish.set()
    thread.join(timeout=10)
    stale = result["response"]
    assert stale.status_code == 409
    assert "stale" in stale.json()["detail"]

    # Retrying now analyzes the new current revision and returns a fresh result.
    monkeypatch.setattr(main_module, "compute_ndvi", real_compute)
    fresh = client.get("/scenes/RACE/ndvi", params={"threshold": 0.3}).json()
    assert fresh["revision"] == 2
    assert fresh["valid_pixel_count"] == 4


def test_missing_and_revision_interleaving_keeps_catalog_working(client, tmp_path):
    red1 = _write_tif(tmp_path / "red1.tif", np.array([[100, 200]], dtype="uint16"))
    nir1 = _write_tif(tmp_path / "nir1.tif", np.array([[300, 100]], dtype="uint16"))
    red3 = _write_tif(tmp_path / "red3.tif", np.array([[500, 600]], dtype="uint16"))
    nir3 = _write_tif(tmp_path / "nir3.tif", np.array([[700, 800]], dtype="uint16"))

    assert _submit(client, "MIX", 1, red1, nir1).status_code == 201
    good = client.get("/scenes/MIX/ndvi", params={"threshold": 0.3})
    assert good.status_code == 200
    mean_rev1 = good.json()["ndvi_mean"]

    # Revision 2 cataloged normally but one band is not a GeoTIFF.
    assert _submit_bytes(client, "MIX", 2, b"BROKEN", red1.read_bytes()).status_code == 201
    broken = client.get("/scenes/MIX/ndvi", params={"threshold": 0.3})
    assert broken.status_code == 422
    assert "GeoTIFF" in broken.json()["detail"]
    # catalog query + downloads continue to serve the current revision
    assert client.get("/scenes/MIX/revisions/2").status_code == 200
    assert client.get("/scenes/MIX/revisions/2/bands/B04/download").content == b"BROKEN"
    assert client.get("/scenes/MIX/revisions/1/bands/B08/download").status_code == 200
    assert [s["scene_id"] for s in client.get("/scenes").json()] == ["MIX"]

    # Revision 3 restores valid bands; analysis works again.
    assert _submit(client, "MIX", 3, red3, nir3).status_code == 201
    fixed = client.get("/scenes/MIX/ndvi", params={"threshold": 0.3})
    assert fixed.status_code == 200
    assert fixed.json()["revision"] == 3
    assert fixed.json()["ndvi_mean"] != mean_rev1

    # Revision 4 has matching grids but different shapes? no: one band is
    # missing resolution, so it is not co-grid; only analysis is rejected.
    bad_grid = _write_tif(
        tmp_path / "nir4.tif", np.ones((3, 3), dtype="uint16")
    )
    assert _submit(client, "MIX", 4, red3, bad_grid).status_code == 201
    assert client.get("/scenes/MIX/ndvi", params={"threshold": 0.3}).status_code == 422
    assert client.get("/scenes/MIX/revisions/3").status_code == 200
    assert client.get("/scenes/MIX/revisions/4/bands/B08/download").status_code == 200


def test_missing_crs_is_grid_error(client, tmp_path):
    red = _write_tif(tmp_path / "red.tif", np.zeros((2, 2), dtype="uint16"), crs=None)
    nir = _write_tif(tmp_path / "nir.tif", np.ones((2, 2), dtype="uint16"), crs=None)
    assert _submit(client, "NOCRS", 1, red, nir).status_code == 201
    r = client.get("/scenes/NOCRS/ndvi", params={"threshold": 0.3})
    assert r.status_code == 422
    assert "CRS" in r.json()["detail"]


def test_block_aligned_windows_for_striped_and_tiled_files():
    """Windows cover the scene exactly and never re-read a block."""
    from app.ndvi import _plan_windows

    for block in ((1, 12000), (256, 256)):
        width, height = 1200, 1000

        class FakeDs:
            block_shapes = [block]

            def __getattr__(self, name):
                if name in ("width", "height"):
                    return width if name == "width" else height
                raise AttributeError(name)

        red = nir = FakeDs()
        windows = _plan_windows(red, nir, 512 * 512)
        coverage = sum(int(w.width) * int(w.height) for w in windows)
        assert coverage == width * height
        assert all(int(w.width) * int(w.height) <= 512 * 512 for w in windows)
        if block[0] == 1:
            # striped file: full-width bands, never split columns
            assert all(int(w.width) == width for w in windows)
        else:
            # tiled file: starts on tile boundaries
            assert all(int(w.col_off) % 256 == 0 and int(w.row_off) % 256 == 0
                       for w in windows)
