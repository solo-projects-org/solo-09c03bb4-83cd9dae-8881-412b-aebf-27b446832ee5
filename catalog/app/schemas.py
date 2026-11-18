"""Pydantic response models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

BandName = Literal["B04", "B08"]


class BandOut(BaseModel):
    band: BandName
    sha256: str
    size_bytes: int
    download_url: str


class RevisionOut(BaseModel):
    scene_id: str
    revision: int
    acquisition_time: str
    west: float
    south: float
    east: float
    north: float
    bands: list[BandOut]
    # Only meaningful on the POST that stored the revision; null on GET.
    submission_status: Optional[
        Literal["created", "idempotent_replay"]
    ] = Field(default=None,
              description="created = stored now; idempotent_replay = identical revision already existed; null = fetched via GET")


class SceneSummary(BaseModel):
    scene_id: str
    revision: int
    acquisition_time: str
    west: float
    south: float
    east: float
    north: float
    bands: list[BandOut]


class NdviOut(BaseModel):
    scene_id: str
    revision: int
    acquisition_time: str
    threshold: float = Field(ge=-1, le=1)
    valid_pixel_count: int = Field(
        ge=0, description="Pixels with a defined NDVI (all exclusions applied)"
    )
    ndvi_mean: Optional[float] = Field(
        default=None, description="Null when no pixel is valid"
    )
    fraction_below_threshold: Optional[float] = Field(
        default=None,
        ge=0,
        le=1,
        description="Share of valid pixels whose NDVI is below the threshold; null when no pixel is valid",
    )


class HealthOut(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]
