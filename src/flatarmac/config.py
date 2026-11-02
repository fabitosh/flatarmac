"""Regions and data locations."""

from dataclasses import dataclass
from pathlib import Path

from pyproj import Transformer

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
RAW_DIR = DATA_DIR / "raw"

LV95 = "EPSG:2056"
WGS84 = "EPSG:4326"


@dataclass(frozen=True)
class Region:
    """Rectangular region in LV95 metres, aligned to the 1 km swissALTI3D tile grid."""

    name: str
    east_min: int
    north_min: int
    east_max: int
    north_max: int

    @property
    def bbox_lv95(self) -> tuple[int, int, int, int]:
        return self.east_min, self.north_min, self.east_max, self.north_max

    @property
    def bbox_wgs84(self) -> tuple[float, float, float, float]:
        """(west, south, east, north), covering the full LV95 rectangle."""
        to_wgs = Transformer.from_crs(LV95, WGS84, always_xy=True)
        lons, lats = to_wgs.transform(
            [self.east_min, self.east_min, self.east_max, self.east_max],
            [self.north_min, self.north_max, self.north_min, self.north_max],
        )
        return min(lons), min(lats), max(lons), max(lats)

    @property
    def dir(self) -> Path:
        return DATA_DIR / self.name


ZURICH = Region("zurich", 2_675_000, 1_238_000, 2_692_000, 1_255_000)

REGIONS = {r.name: r for r in (ZURICH,)}
