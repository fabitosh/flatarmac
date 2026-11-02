"""Gradient profiles along edges, from swissTLM3D Z values.

TLM Z follows the terrain model on normal roads and the deck on bridges / underpasses, so no DEM
correction is needed for engineering structures.
"""

from dataclasses import dataclass

import numpy as np
import shapely

SAMPLE_SPACING_M = 10.0
GRADE_WINDOW_M = 50.0


@dataclass(frozen=True)
class Profile:
    distance_m: np.ndarray  # sample positions along the edge
    z: np.ndarray
    grade: np.ndarray  # smoothed grade per sample interval (len = samples - 1)


def profile(line: shapely.LineString) -> Profile:
    coords = shapely.get_coordinates(line, include_z=True)
    steps = np.hypot(np.diff(coords[:, 0]), np.diff(coords[:, 1]))
    along = np.concatenate([[0.0], np.cumsum(steps)])
    length = along[-1]

    n_samples = max(int(np.ceil(length / SAMPLE_SPACING_M)), 1) + 1
    distance = np.linspace(0.0, length, n_samples)
    z = np.interp(distance, along, coords[:, 2])
    if length == 0:
        return Profile(distance, z, np.zeros(0))

    # Grade over a centred window, clamped to the edge. Short edges get their end-to-end grade.
    mid = (distance[:-1] + distance[1:]) / 2
    half = min(GRADE_WINDOW_M, length) / 2
    lo = np.clip(mid - half, 0.0, length - 2 * half)
    hi = lo + 2 * half
    grade = (np.interp(hi, along, coords[:, 2]) - np.interp(lo, along, coords[:, 2])) / (2 * half)
    return Profile(distance, z, grade)


def drape(line: shapely.LineString, dem) -> shapely.LineString:
    """Add Z from an open rasterio DEM, sampled every SAMPLE_SPACING_M."""
    distance = np.linspace(0, line.length, max(int(line.length / SAMPLE_SPACING_M), 1) + 1)
    xy = shapely.get_coordinates(shapely.line_interpolate_point(line, distance))
    z = np.array([v[0] for v in dem.sample(xy)])
    return shapely.LineString(np.column_stack([xy, z]))


def summarise(line: shapely.LineString) -> dict[str, float]:
    p = profile(line)
    dz = np.diff(p.z)
    return {
        "ascent_m": float(dz[dz > 0].sum()),
        "descent_m": float(-dz[dz < 0].sum()),
        "max_abs_grade": float(np.abs(p.grade).max()) if len(p.grade) else 0.0,
    }
