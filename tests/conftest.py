"""共享 fixtures."""

from __future__ import annotations

import numpy as np
import pytest

from tsn.core.config import EngineConfig
from tsn.core.models import ExerciseSetting
from tsn.engine import geo
from tsn.engine.track import TrackOptions


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(20260101)


@pytest.fixture
def square_route() -> list[geo.Coord]:
    """一个 400m 左右的方形校园环路（经度在前）."""
    base_lng, base_lat = 104.06, 30.67
    d = 0.0009  # 约 100m
    return [
        (base_lng, base_lat),
        (base_lng + d, base_lat),
        (base_lng + d, base_lat + d),
        (base_lng, base_lat + d),
        (base_lng, base_lat),
    ]


@pytest.fixture
def setting() -> ExerciseSetting:
    return ExerciseSetting(
        identify="ident-test",
        total_range_m=2000.0,
        start_qualified_s=600.0,     # 10 分钟
        end_qualified_s=1800.0,      # 30 分钟
        step_required=2500,
        step_recursive=True,
        geofence=[],
        checkpoints=[],
    )


@pytest.fixture
def track_options() -> TrackOptions:
    opts = TrackOptions.from_config(EngineConfig())
    opts.gps_sigma_m = 3.0
    return opts
