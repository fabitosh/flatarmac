"""OpenStreetMap features that interrupt a run (signals, crossings, barriers), running tracks, and
what lies beside a road (sidewalks, pedestrian areas, shops)."""

import geopandas as gpd
import httpx
import pandas as pd
import shapely
from shapely.geometry import LineString, Point

from flatarmac.config import LV95, WGS84, Region

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "flatarmac/0.1 (personal running route research)"

STOP_NODE_FILTERS = [
    '["highway"="traffic_signals"]',
    '["highway"="crossing"]',
    '["crossing"]',
    '["highway"="stop"]',
    '["highway"="give_way"]',
    '["railway"="level_crossing"]',
    '["railway"="crossing"]',
    '["barrier"~"^(gate|lift_gate|swing_gate|stile|kissing_gate|turnstile|full-height_turnstile)$"]',
]
NODE_TAGS = ["highway", "crossing", "crossing:signals", "railway", "barrier", "access", "foot"]


def _bbox(region: Region) -> str:
    west, south, east, north = region.bbox_wgs84
    return f"{south},{west},{north},{east}"


def _stops_query(region: Region) -> str:
    nodes = "\n".join(f"  node{f}({_bbox(region)});" for f in STOP_NODE_FILTERS)
    return f"[out:json][timeout:180];\n(\n{nodes}\n);\nout body;"


def _tracks_query(region: Region) -> str:
    bbox = _bbox(region)
    return f"""[out:json][timeout:120];
(
  way["leisure"="track"]({bbox});
  relation["leisure"="track"]({bbox});
);
out body geom;"""


def _lap_ring(element: dict) -> LineString | None:
    """Ring a runner laps. Ways: the way itself. Track relations (multipolygons): the largest
    inner ring, i.e. the lane-1 kerb; the outer ring only when there is no inner one."""
    if element["type"] == "way":
        return LineString([(p["lon"], p["lat"]) for p in element["geometry"]])
    for role in ("inner", "outer"):
        lines = [
            LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
            for m in element.get("members", [])
            if m["type"] == "way" and m.get("role") == role and len(m.get("geometry", [])) > 1
        ]
        polygons = list(shapely.polygonize(lines).geoms) if lines else []
        if polygons:
            # Area in degrees² is fine for picking the largest ring.
            return max(polygons, key=lambda p: p.area).exterior
    return None


def _overpass(query: str) -> list[dict]:
    errors = []
    for url in OVERPASS_URLS:
        try:
            response = httpx.post(
                url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=240
            )
            response.raise_for_status()
            return response.json()["elements"]
        except (httpx.HTTPError, ValueError) as error:
            errors.append(f"{url}: {error}")
    raise RuntimeError("all Overpass endpoints failed:\n" + "\n".join(errors))


def fetch_stops(region: Region) -> gpd.GeoDataFrame:
    node_rows = [
        {
            "osm_id": e["id"],
            **{t: e["tags"].get(t) for t in NODE_TAGS},
            "geometry": Point(e["lon"], e["lat"]),
        }
        for e in _overpass(_stops_query(region))
        if e["type"] == "node"
    ]
    stops = gpd.GeoDataFrame(pd.DataFrame(node_rows), crs=WGS84).to_crs(LV95)
    region.dir.mkdir(parents=True, exist_ok=True)
    stops.to_parquet(region.dir / "osm_stops.parquet")
    return stops


def fetch_tracks(region: Region) -> gpd.GeoDataFrame:
    track_rows = [
        {
            "osm_type": e["type"],
            "osm_id": e["id"],
            "sport": e["tags"].get("sport"),
            "surface": e["tags"].get("surface"),
            "access": e["tags"].get("access"),
            "name": e["tags"].get("name"),
            "length_tag": e["tags"].get("length"),
            "geometry": ring,
        }
        for e in _overpass(_tracks_query(region))
        if e["type"] in ("way", "relation") and (ring := _lap_ring(e)) is not None
    ]
    tracks = gpd.GeoDataFrame(pd.DataFrame(track_rows), crs=WGS84).to_crs(LV95)
    region.dir.mkdir(parents=True, exist_ok=True)
    tracks.to_parquet(region.dir / "osm_tracks.parquet")
    return tracks


ROAD_HIGHWAYS = (
    "trunk|trunk_link|primary|primary_link|secondary|secondary_link|tertiary|tertiary_link"
    "|unclassified|residential|living_street|service|pedestrian"
)
FOOD = "restaurant|cafe|fast_food|bar|pub|ice_cream"
WALK_TAGS = ["highway", "area", "sidewalk", "sidewalk:both", "sidewalk:left", "sidewalk:right"]


def _walk_query(region: Region) -> str:
    bbox = _bbox(region)
    return f"""[out:json][timeout:240];
(
  way["highway"~"^({ROAD_HIGHWAYS})$"]({bbox});
  way["footway"="sidewalk"]({bbox});
);
out tags geom;
(
  node["shop"]({bbox});
  way["shop"]({bbox});
  node["amenity"~"^({FOOD})$"]({bbox});
  way["amenity"~"^({FOOD})$"]({bbox});
  node["railway"="station"]["station"!="subway"]({bbox});
);
out tags center;"""


def _walk_kind(tags: dict) -> str:
    if tags.get("railway") == "station":
        return "station"
    if "shop" in tags or "amenity" in tags:
        return "poi"
    if tags.get("footway") == "sidewalk":
        return "sidewalk"
    if tags.get("highway") == "pedestrian" and tags.get("area") == "yes":
        return "pedestrian_area"
    return "road"


def _walk_geometry(element: dict, kind: str):
    if "center" in element:
        return Point(element["center"]["lon"], element["center"]["lat"])
    if element["type"] == "node":
        return Point(element["lon"], element["lat"])
    coords = [(p["lon"], p["lat"]) for p in element["geometry"]]
    if kind == "pedestrian_area" and len(coords) >= 4:
        return shapely.Polygon(coords)
    return LineString(coords) if len(coords) > 1 else None


def fetch_walk(region: Region) -> gpd.GeoDataFrame:
    """Roads with sidewalk tags, separately mapped sidewalks, pedestrian streets and areas, shops,
    cafés and restaurants, and railway stations: what makes running beside a road easy or crowded."""
    rows = []
    for e in _overpass(_walk_query(region)):
        tags = e.get("tags", {})
        kind = _walk_kind(tags)
        geometry = _walk_geometry(e, kind)
        if geometry is not None:
            rows.append({"kind": kind, **{t: tags.get(t) for t in WALK_TAGS}, "geometry": geometry})
    walk = gpd.GeoDataFrame(pd.DataFrame(rows), crs=WGS84).to_crs(LV95)
    region.dir.mkdir(parents=True, exist_ok=True)
    walk.to_parquet(region.dir / "osm_walk.parquet")
    return walk


def fetch(region: Region) -> None:
    fetch_stops(region)
    fetch_tracks(region)
    fetch_walk(region)
