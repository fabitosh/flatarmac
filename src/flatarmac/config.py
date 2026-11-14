"""Regions and data locations."""

import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import platformdirs
from pyproj import Transformer

PACKAGE_DIR = Path(__file__).resolve().parent
# Running from a git checkout (`uv run`): keep data and curated lists next to the code.
CHECKOUT_DIR = PACKAGE_DIR.parents[1]
FROM_CHECKOUT = (CHECKOUT_DIR / "pyproject.toml").exists()


def _data_dir() -> Path:
    if env := os.environ.get("FLATARMAC_DATA"):
        return Path(env).expanduser()
    if FROM_CHECKOUT:
        return CHECKOUT_DIR / "data"
    return Path(platformdirs.user_data_dir("flatarmac", appauthor=False))


DATA_DIR = _data_dir()
RAW_DIR = DATA_DIR / "raw"
# Hand-edited lists shipped with flatarmac; edits go to the checkout or the data dir.
BUNDLED_CURATED_DIR = PACKAGE_DIR / "curated"
CURATED_DIR = BUNDLED_CURATED_DIR if FROM_CHECKOUT else DATA_DIR / "curated"

LV95 = "EPSG:2056"
WGS84 = "EPSG:4326"
MAX_RADIUS_KM = 15


@dataclass(frozen=True)
class Region:
    """Rectangular region in LV95 metres, aligned to the 1 km swissALTI3D tile grid."""

    name: str
    east_min: int
    north_min: int
    east_max: int
    north_max: int
    # Municipality left out of curated track names; tracks elsewhere get theirs appended.
    city: str = ""

    @classmethod
    def around(cls, name: str, east: float, north: float, radius_km: float, city: str = ""):
        """Square of whole 1 km tiles covering `radius_km` around an LV95 point."""
        if not 0 < radius_km <= MAX_RADIUS_KM:
            raise ValueError(f"radius must be between 0 and {MAX_RADIUS_KM} km")
        r = radius_km * 1000
        e0, n0 = (math.floor((v - r) / 1000) * 1000 for v in (east, north))
        e1, n1 = (math.ceil((v + r) / 1000) * 1000 for v in (east, north))
        return cls(name, e0, n0, e1, n1, city)

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

    @property
    def is_built(self) -> bool:
        return (self.dir / "web" / "edges.geojson").exists()

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "region.json").write_text(json.dumps(asdict(self), ensure_ascii=False) + "\n")


ZURICH = Region("zurich", 2_675_000, 1_238_000, 2_692_000, 1_255_000, city="Zürich")


def regions() -> dict[str, Region]:
    """Built-in regions plus those set up with `flatarmac setup`."""
    found = {ZURICH.name: ZURICH}
    for path in sorted(DATA_DIR.glob("*/region.json")):
        region = Region(**json.loads(path.read_text()))
        found[region.name] = region
    return found


def curated_path(kind: str, region: Region) -> Path:
    """`<kind>_<region>.csv`: the user's edited copy if there is one, else the shipped one."""
    name = f"{kind}_{region.name}.csv"
    own = CURATED_DIR / name
    return own if own.exists() else BUNDLED_CURATED_DIR / name
