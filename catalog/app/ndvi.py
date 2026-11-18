"""Windowed NDVI analysis for one scene revision.

Both bands are ordinary GeoTIFF files in the catalog's content-addressed
store.  They are read in windows sized to the file's block layout so peak
memory is bounded by the window instead of the scene size.  Each band's own ``scale``/``offset``
metadata is applied to convert raw DN to reflectance; when no metadata is
present the raw values are used unchanged.

Pixels are excluded when either band has NoData / an invalid mask, when a
converted reflectance is non-finite or negative, or when the NDVI denominator
(B08 + B04) is zero.  All arithmetic is done in float64.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import rasterio
from rasterio.errors import RasterioError
from rasterio.windows import Window

# Target number of pixels per read window.  Windows are aligned to the file's
# own block layout so each decoded block is used once; the budget only bounds
# the working set (a 256k-pixel window is ~2 MiB per float64 array).
WINDOW_PIXELS = 512 * 512

# Bound GDAL's block cache while analysing so a huge scene cannot make the
# process buffer large amounts of decoded band data.  Overridable via the
# standard GDAL_CACHEMAX environment variable.
GDAL_CACHE_MB = 16

_METADATA_PRECISION = 1e-9


class AnalysisError(Exception):
    """The requested analysis cannot be performed (HTTP 422 for this call)."""


@dataclass(frozen=True)
class NdviStats:
    valid_pixel_count: int
    ndvi_mean: Optional[float]
    fraction_below_threshold: Optional[float]


def _plan_windows(
    red: rasterio.DatasetReader,
    nir: rasterio.DatasetReader,
    max_pixels: int,
) -> list[Window]:
    """Split the scene into windows aligned with the bands' block layout.

    A TIFF block is the smallest unit GDAL decodes, so windows are whole
    numbers of blocks in both directions.  This avoids decoding any block
    twice, while ``max_pixels`` caps the working set per window (a 256k-pixel
    window is ~2 MiB per float64 array).
    """
    block_h = max(red.block_shapes[0][0], nir.block_shapes[0][0], 1)
    block_w = max(red.block_shapes[0][1], nir.block_shapes[0][1], 1)

    if block_h * block_w > max_pixels:
        # A single decoded block is already over the budget (e.g. mismatched
        # layouts): fall back to plain budget-sized windows and accept that
        # GDAL may decode some blocks more than once.
        rows_per_window = max(1, max_pixels // max(red.width, 1))
        cols_per_window = max(1, max_pixels // rows_per_window)
    else:
        # Striped files have one block per full-width row; only the number of
        # rows is reducible.  Tiled files are read in whole tile rows.
        if block_h == 1:
            rows_per_window = max(1, max_pixels // max(red.width, 1))
        else:
            rows_per_window = block_h
        rows_per_window = min(red.height, rows_per_window)
        cols_budget = max(1, max_pixels // rows_per_window)
        if block_w <= cols_budget:
            cols_per_window = (cols_budget // block_w) * block_w
        else:
            # Full-width blocks exceed the budget: split them into budget-sized
            # column windows (only their decoded rows are large, not our arrays).
            cols_per_window = cols_budget

    rows_per_window = min(red.height, rows_per_window)
    cols_per_window = min(red.width, cols_per_window)

    windows = []
    for row_off in range(0, red.height, rows_per_window):
        rows = min(rows_per_window, red.height - row_off)
        for col_off in range(0, red.width, cols_per_window):
            cols = min(cols_per_window, red.width - col_off)
            windows.append(Window(col_off, row_off, cols, rows))
    return windows


def _validate_band(ds: rasterio.DatasetReader, label: str) -> None:
    if ds.driver != "GTiff":
        raise AnalysisError(
            f"{label} is not a GeoTIFF (raster driver is {ds.driver!r})"
        )
    if ds.count != 1:
        raise AnalysisError(
            f"{label} must have exactly one band, found {ds.count}"
        )


def _validate_same_grid(
    red: rasterio.DatasetReader, nir: rasterio.DatasetReader
) -> None:
    problems = []
    if red.crs is None or nir.crs is None:
        missing = [name for name, ds in (("B04", red), ("B08", nir)) if ds.crs is None]
        raise AnalysisError(
            "band file has no CRS, so it has no defined grid: " + ", ".join(missing)
        )
    if (red.width, red.height) != (nir.width, nir.height):
        problems.append(
            f"size B04={red.width}x{red.height} vs B08={nir.width}x{nir.height}"
        )
    if red.crs != nir.crs:
        problems.append(f"CRS B04={red.crs} vs B08={nir.crs}")
    # A pixel-grid comparison has to include the affine transform (origin,
    # pixel size and any rotation), not just the raster dimensions.
    if not red.transform.almost_equals(
        nir.transform, precision=_METADATA_PRECISION
    ):
        problems.append(f"transform B04={red.transform} vs B08={nir.transform}")
    if problems:
        raise AnalysisError("B04 and B08 are not on the same grid: " + "; ".join(problems))


def _parse_float(value: object, label: str, band: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise AnalysisError(
            f"{band} has a non-numeric {label} value: {value!r}"
        ) from exc
    if not math.isfinite(parsed):
        raise AnalysisError(f"{band} has a non-finite {label} value: {value!r}")
    return parsed


def _scale_offset(ds: rasterio.DatasetReader, band: str) -> tuple[float, float]:
    """Return (scale, offset), defaulting to 1/0 ("use raw values").

    GDAL exposes band scale/offset through ``ds.scales``/``ds.offsets``.  Some
    producers instead write them as ordinary band tags, so fall back to those
    keys when the GDAL metadata is at its default.
    """
    scale = float(ds.scales[0]) if ds.scales else 1.0
    offset = float(ds.offsets[0]) if ds.offsets else 0.0

    tags = {str(key).lower(): value for key, value in ds.tags(1).items()}
    if scale == 1.0 and "scale" in tags:
        scale = _parse_float(tags["scale"], "scale", band)
    if offset == 0.0 and "offset" in tags:
        offset = _parse_float(tags["offset"], "offset", band)

    if not math.isfinite(scale) or not math.isfinite(offset):
        raise AnalysisError(f"{band} has non-finite scale/offset metadata")
    return scale, offset


def _to_reflectance(
    masked: np.ma.MaskedArray, scale: float, offset: float
) -> tuple[np.ndarray, np.ndarray]:
    """Convert raw DN to float64 reflectance and return (values, mask)."""
    invalid = np.ma.getmaskarray(masked).copy()
    values = np.asarray(np.ma.getdata(masked), dtype=np.float64)
    values *= scale
    values += offset
    invalid |= ~np.isfinite(values)
    return values, invalid


def compute_ndvi(
    b04_path: str,
    b08_path: str,
    threshold: float,
    window_pixels: int = WINDOW_PIXELS,
) -> NdviStats:
    """Compute NDVI statistics over the full extent of a B04/B08 pair.

    Raises :class:`AnalysisError` when either file is not a single-band
    GeoTIFF, or when the two bands are not on the same grid.
    """
    # GDAL otherwise buffers decoded blocks in a large cache; capping it keeps
    # total memory bounded by the window size even for very large scenes.
    # It must be configured before GDAL reads anything (a rasterio.Env scoped
    # around the reads is markedly slower), and must not override an explicit
    # operator setting.
    if os.environ.get("GDAL_CACHEMAX") is None:
        os.environ["GDAL_CACHEMAX"] = str(GDAL_CACHE_MB)

    try:
        red = rasterio.open(b04_path)
    except (RasterioError, OSError) as exc:
        raise AnalysisError(f"B04 is not a readable GeoTIFF: {exc}") from exc

    try:
        try:
            nir = rasterio.open(b08_path)
        except (RasterioError, OSError) as exc:
            raise AnalysisError(f"B08 is not a readable GeoTIFF: {exc}") from exc

        with nir:
            _validate_band(red, "B04")
            _validate_band(nir, "B08")
            _validate_same_grid(red, nir)

            red_scale, red_offset = _scale_offset(red, "B04")
            nir_scale, nir_offset = _scale_offset(nir, "B08")

            valid_count = 0
            ndvi_sum = 0.0
            below_count = 0

            try:
                for window in _plan_windows(red, nir, window_pixels):
                    red_block = red.read(1, window=window, masked=True)
                    nir_block = nir.read(1, window=window, masked=True)

                    red_values, invalid = _to_reflectance(
                        red_block, red_scale, red_offset
                    )
                    nir_values, nir_invalid = _to_reflectance(
                        nir_block, nir_scale, nir_offset
                    )
                    invalid |= nir_invalid

                    # Negative reflectance and a zero denominator are not
                    # physically meaningful NDVI inputs.
                    invalid |= (red_values < 0.0) | (nir_values < 0.0)
                    denominator = nir_values + red_values
                    invalid |= denominator == 0.0

                    valid = ~invalid
                    block_count = int(np.count_nonzero(valid))
                    if block_count == 0:
                        continue

                    block_ndvi = (
                        nir_values[valid] - red_values[valid]
                    ) / denominator[valid]
                    valid_count += block_count
                    ndvi_sum += float(block_ndvi.sum(dtype=np.float64))
                    below_count += int(np.count_nonzero(block_ndvi < threshold))
            except (RasterioError, OSError, ValueError) as exc:
                # A file can have a valid TIFF header and still be truncated
                # or corrupt; that only makes this analysis fail.
                raise AnalysisError(
                    f"failed to read band data: {exc}"
                ) from exc
    finally:
        red.close()

    if valid_count == 0:
        return NdviStats(0, None, None)
    return NdviStats(
        valid_pixel_count=valid_count,
        ndvi_mean=ndvi_sum / valid_count,
        fraction_below_threshold=below_count / valid_count,
    )
