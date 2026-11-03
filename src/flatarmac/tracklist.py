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
from flatarmac.sources.osm import USER_AGENT, _lap_ring, _overpass

CURATED_DIR = Path(__file__).resolve().parents[2] / "curated"
OSM_API = "https://api.openstreetmap.org/api/0.6"
COLUMNS = ["osm_type", "osm_id", "name", "lap_m", "surface", "include", "note"]
SPORTS = {"running", "athletics"}
MIN_M, MAX_M = 200.0, 2000.0
# 4πA/P²: circle 1, stadium oval ~0.75, elongated forest loop ~0.3, sprint strip outline ≤ 0.15.
MIN_ROUNDNESS = 0.45
STRIP_ROUNDNESS = 0.2
# Forest loops (Finnenbahn, Vita parcours) are soft and hilly: not for track workouts.
TRAIL_SURFACES = {"woodchips", "wood", "bark", "grass", "dirt", "ground"}
TRAIL_NAMES = ("finnenbahn", "vita")
# Track names that say what, not where ("100m Bahn"): prefixed to the site name instead.
GENERIC_NAMES = {"finnenbahn", "vitaparcour", "vitaparcours", "rundbahn", "laufbahn", "100m bahn"}
# Named places a track belongs to, most specific first.
SITE_KINDS = ["stadium", "sports_centre", "recreation_ground", "school", "college", "university"]
SITE_FILTER = '[name][~"^(leisure|landuse|amenity)$"~"^(' + "|".join(SITE_KINDS) + ')$"]'
SITE_RADIUS_M = 150


def is_trail_loop(name: str, surface: str) -> bool:
    return surface in TRAIL_SURFACES or any(n in name.lower() for n in TRAIL_NAMES)


def round_lap(ring_m: float) -> float:
    """Mapped rings are a few % off what is signposted: snap to a round 50 m if that close."""
    fifty = round(ring_m / 50) * 50
    return float(fifty) if abs(ring_m - fifty) <= 0.03 * fifty else float(round(ring_m / 10) * 10)


def track_name(own: str, site: str, municipality: str, street: str, city: str) -> str:
    """OSM name if it says where; else the school or sports ground it is in; else nearest street."""
    if own and own.lower() not in GENERIC_NAMES:
        name = own
    elif own:
        name = f"{own} {site or municipality}".strip()
    else:
        name = site or f"Track near {street}"
    if municipality and municipality != city and municipality not in name:
        name += f", {municipality}"
    return name


def _site_kind(tags: dict) -> int:
    kinds = [tags.get(k) for k in ("leisure", "landuse", "amenity")]
    return min(SITE_KINDS.index(k) for k in kinds if k in SITE_KINDS)


def sites(points: list[shapely.Point]) -> list[tuple[str, str]]:
    """(site, municipality) per WGS84 point: the named sports ground or school containing it,
    else the nearest one within SITE_RADIUS_M. One Overpass request for all points."""
    query = "[out:json][timeout:600];"
    for p in points:
        query += (
            f"is_in({p.y},{p.x})->.a;"
            f"area.a{SITE_FILTER};out tags;out count;"
            f"nwr(around:{SITE_RADIUS_M},{p.y},{p.x}){SITE_FILTER};out tags center;out count;"
            'area.a["admin_level"="8"];out tags;out count;'
        )
    groups, group = [], []
    for e in _overpass(query):
        if e["type"] == "count":
            groups.append(group)
            group = []
        else:
            group.append(e)

    result = []
    for i, p in enumerate(points):
        inside, near, admin = groups[3 * i : 3 * i + 3]

        def dist(e, p=p):
            c = e.get("center", e)
            return math.hypot((c["lon"] - p.x) * math.cos(math.radians(p.y)), c["lat"] - p.y)

        best = min(inside, key=lambda e: _site_kind(e["tags"]), default=None)
        best = best or min(near, key=dist, default=None)
        site = best["tags"]["name"] if best else ""
        result.append((site, admin[0]["tags"]["name"] if admin else ""))
    return result


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
    # 400 m ovals are mapped somewhere between lane-1 kerb and outer edge, as sport=athletics
    # or sport=running alike.
    trail = is_trail_loop(tags.get("name", ""), tags.get("surface", ""))
    if not trail and 360 <= ring_m <= 480:
        return 400.0
    return round_lap(ring_m)


def _strip_m(ring_lv95) -> float:
    """Length of a sprint strip: the long side of its outline's bounding rectangle."""
    rect = shapely.Polygon(ring_lv95).minimum_rotated_rectangle.exterior.coords
    return max(math.dist(rect[0], rect[1]), math.dist(rect[1], rect[2]))


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

    def nearby_street(ring_lv95) -> str:
        return named["name"].iloc[named_tree.nearest(ring_lv95.centroid)]

    new_rows, new_rings, centroids = [], [], []
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
            trail = is_trail_loop(tags.get("name", ""), tags.get("surface", ""))
            # A thin outline is no lap anyone runs: a sprint strip if it goes around once,
            # else something odd like a track drawn as one line along both edges.
            thin = roundness < STRIP_ROUNDNESS
            lap_m = None if thin else _default_lap(tags, ring_m)
            note = f"ring {ring_m:.0f} m; roundness {roundness:.2f}; sport={tags.get('sport', '')}"
            if thin and ring_m < 2.5 * (strip_m := _strip_m(ring_lv95)):
                note += f"; sprint strip ~{strip_m:.0f} m, not a loop"
            elif thin:
                note += f"; thin outline ~{strip_m:.0f} m long, lap unclear"
            centroids.append(ring.centroid)
            new_rows.append(
                {
                    "osm_type": osm_type,
                    "osm_id": osm_id,
                    "name": (tags.get("name", ""), nearby_street(ring_lv95)),
                    "lap_m": lap_m,
                    "surface": tags.get("surface", ""),
                    "include": sport_ok
                    and not thin
                    and MIN_M <= min(ring_m, lap_m)
                    and ring_m <= MAX_M
                    and roundness >= MIN_ROUNDNESS
                    and not trail,
                    "note": note + ("; trail loop" if trail else ""),
                }
            )

    for row, (site, municipality) in zip(
        new_rows, sites(centroids) if centroids else [], strict=True
    ):
        own, street = row["name"]
        row["name"] = track_name(own, site, municipality, street, region.city)

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
