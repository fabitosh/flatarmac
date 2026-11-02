"""Curated list of running tracks per region, with the lap length a runner actually runs.

The CSV in `curated/tracks_<region>.csv` is the source of truth and meant to be edited by hand
(`lap_m`, `include`, `name`, `note`). `refresh` adds newly discovered OSM tracks without touching
existing rows. Ring geometry is fetched per id from the main OSM API (no Overpass needed).
"""

import math
import re
from pathlib import Path

import geopandas as gpd
import httpx
import pandas as pd
import shapely

from flatarmac.config import LV95, WGS84, Region
from flatarmac.sources.osm import USER_AGENT, _lap_ring

CURATED_DIR = Path(__file__).resolve().parents[2] / "curated"
OSM_API = "https://api.openstreetmap.org/api/0.6"
COLUMNS = ["osm_type", "osm_id", "name", "lap_m", "surface", "include", "note"]
SPORTS = {"running", "athletics"}
MIN_M, MAX_M = 200.0, 2000.0
# 4πA/P²: circle 1, stadium oval ~0.75, outline of a 100 m sprint strip ~0.25.
MIN_ROUNDNESS = 0.45


def csv_path(region: Region) -> Path:
    return CURATED_DIR / f"tracks_{region.name}.csv"


def ring_cache(region: Region) -> Path:
    return region.dir / "track_rings.parquet"


def load(region: Region) -> pd.DataFrame:
    path = csv_path(region)
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(path, dtype={"osm_type": str, "osm_id": int, "include": bool})


def _fetch_element(client: httpx.Client, osm_type: str, osm_id: int) -> dict:
    """Fetch one way/relation with full geometry and shape it like an Overpass `out geom` element."""
    elements = client.get(f"{OSM_API}/{osm_type}/{osm_id}/full.json").json()["elements"]
    nodes = {e["id"]: e for e in elements if e["type"] == "node"}
    ways = {e["id"]: e for e in elements if e["type"] == "way"}

    def geom(way_id):
        return [{"lat": nodes[n]["lat"], "lon": nodes[n]["lon"]} for n in ways[way_id]["nodes"]]

    root = next(e for e in elements if e["type"] == osm_type and e["id"] == osm_id)
    if osm_type == "way":
        return {**root, "geometry": geom(osm_id)}
    members = [
        {**m, "geometry": geom(m["ref"])}
        for m in root["members"]
        if m["type"] == "way" and m["ref"] in ways
    ]
    return {**root, "members": members}


def _default_lap(tags: dict, ring_m: float) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m)?\s*", tags.get("length", ""))
    if match:
        return float(match[1])
    # Athletics rings are mapped somewhere between lane-1 kerb and outer edge.
    if "athletics" in tags.get("sport", "") and 360 <= ring_m <= 480:
        return 400.0
    return round(ring_m / 5) * 5


def refresh(region: Region, extra_ids: list[tuple[str, int]] = ()) -> pd.DataFrame:
    """Add candidates from the last Overpass fetch and `extra_ids`; fetch missing ring geometry."""
    curated = load(region)
    known = set(zip(curated["osm_type"], curated["osm_id"], strict=True))

    candidates = set(extra_ids)
    osm_path = region.dir / "osm_tracks.parquet"
    if osm_path.exists():
        osm = gpd.read_parquet(osm_path)
        osm_type = osm["osm_type"] if "osm_type" in osm else pd.Series("way", index=osm.index)
        sport_ok = osm["sport"].fillna("").str.split(";").apply(lambda s: bool(SPORTS & set(s)))
        closed = shapely.is_closed(osm.geometry.values)
        sized = osm.length.between(MIN_M, MAX_M)
        keep = sport_ok & (closed | (osm_type == "relation")) & sized
        candidates |= set(zip(osm_type[keep], osm["osm_id"][keep], strict=True))

    cache_path = ring_cache(region)
    rings = gpd.read_parquet(cache_path) if cache_path.exists() else None
    cached = set() if rings is None else set(zip(rings["osm_type"], rings["osm_id"], strict=True))

    edges = gpd.read_parquet(region.dir / "edges.parquet", columns=["name", "geometry"])
    named = edges[edges["name"].notna()].reset_index(drop=True)
    named_tree = shapely.STRtree(named.geometry.values)

    def nearby_name(ring_lv95) -> str:
        return f"Track near {named['name'].iloc[named_tree.nearest(ring_lv95.centroid)]}"

    new_rows, new_rings = [], []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60) as client:
        for osm_type, osm_id in sorted(candidates | known):
            if (osm_type, osm_id) in cached and (osm_type, osm_id) in known:
                continue
            element = _fetch_element(client, osm_type, osm_id)
            ring = _lap_ring(element)
            if ring is None:
                continue
            ring_lv95 = gpd.GeoSeries([ring], crs=WGS84).to_crs(LV95).iloc[0]
            ring_m = ring_lv95.length
            tags = element.get("tags", {})
            new_rings.append({"osm_type": osm_type, "osm_id": osm_id, "geometry": ring})
            if (osm_type, osm_id) in known:
                continue
            sport_ok = bool(SPORTS & set(tags.get("sport", "").split(";")))
            roundness = 4 * math.pi * shapely.Polygon(ring_lv95).area / ring_m**2
            # Forest woodchip loops (Finnenbahnen) are irregular but real loops.
            forest_loop = tags.get("surface") == "woodchips" or "finnenbahn" in (
                tags.get("name", "").lower()
            )
            lap_m = _default_lap(tags, ring_m)
            new_rows.append(
                {
                    "osm_type": osm_type,
                    "osm_id": osm_id,
                    "name": tags.get("name") or nearby_name(ring_lv95),
                    "lap_m": lap_m,
                    "surface": tags.get("surface", ""),
                    "include": sport_ok
                    and MIN_M <= min(ring_m, lap_m)
                    and ring_m <= MAX_M
                    and (roundness >= MIN_ROUNDNESS or forest_loop),
                    "note": f"ring {ring_m:.0f} m; roundness {roundness:.2f}; "
                    f"sport={tags.get('sport', '')}",
                }
            )

    if new_rings:
        fresh = gpd.GeoDataFrame(new_rings, crs=WGS84).to_crs(LV95)
        rings = fresh if rings is None else pd.concat([rings, fresh], ignore_index=True)
        rings = rings.drop_duplicates(["osm_type", "osm_id"], keep="last")
        rings.to_parquet(cache_path)

    curated = pd.concat([curated, pd.DataFrame(new_rows, columns=COLUMNS)], ignore_index=True)
    curated = curated.sort_values(["include", "lap_m"], ascending=[False, True])
    CURATED_DIR.mkdir(exist_ok=True)
    curated.to_csv(csv_path(region), index=False)
    return curated


def included(region: Region) -> gpd.GeoDataFrame:
    """Curated, included tracks joined with their ring geometry (LV95)."""
    curated = load(region)
    curated = curated[curated["include"]]
    rings = gpd.read_parquet(ring_cache(region))
    return rings.merge(curated, on=["osm_type", "osm_id"], how="inner")
