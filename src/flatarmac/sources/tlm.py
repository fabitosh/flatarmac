"""swissTLM3D roads and paths (TLM_STRASSE)."""

import geopandas as gpd
import httpx
import pyogrio

from flatarmac.config import LV95, RAW_DIR, Region
from flatarmac.sources.remote_zip import extract_members

STAC_ITEMS = "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swisstlm3d/items"
LAYER = "swissTLM3D_TLM_STRASSE"
SHAPEFILE_PARTS = [f"{LAYER}.{ext}" for ext in ("shp", "shx", "dbf", "prj", "cpg")]


def latest_release() -> tuple[str, str]:
    """Return (release id, shapefile zip url) of the newest swissTLM3D release."""
    items = httpx.get(STAC_ITEMS, params={"limit": 100}, timeout=60).json()["features"]
    newest = max(items, key=lambda item: item["id"])
    url = next(a["href"] for k, a in newest["assets"].items() if k.endswith(".shp.zip"))
    return newest["id"], url


def fetch(region: Region) -> gpd.GeoDataFrame:
    release, url = latest_release()
    raw_dir = RAW_DIR / release
    extract_members(url, SHAPEFILE_PARTS, raw_dir)
    roads = pyogrio.read_dataframe(raw_dir / f"{LAYER}.shp", bbox=region.bbox_lv95)
    roads = roads.set_crs(LV95, allow_override=True)
    roads.columns = [c.lower() for c in roads.columns]
    roads.attrs["release"] = release
    region.dir.mkdir(parents=True, exist_ok=True)
    roads.to_parquet(region.dir / "tlm_roads.parquet")
    return roads
