"""OpenStreetMap features that interrupt a run (signals, crossings, barriers) and running tracks."""

import geopandas as gpd
import httpx
import pandas as pd
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


def _query(region: Region) -> str:
    west, south, east, north = region.bbox_wgs84
    bbox = f"{south},{west},{north},{east}"
    nodes = "\n".join(f"  node{f}({bbox});" for f in STOP_NODE_FILTERS)
    return f"""
[out:json][timeout:180];
(
{nodes}
);
out body;
(
  way["leisure"="track"]({bbox});
);
out geom tags;
"""


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


def fetch(region: Region) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    elements = _overpass(_query(region))

    node_rows = [
        {
            "osm_id": e["id"],
            **{t: e["tags"].get(t) for t in NODE_TAGS},
            "geometry": Point(e["lon"], e["lat"]),
        }
        for e in elements
        if e["type"] == "node"
    ]
    stops = gpd.GeoDataFrame(pd.DataFrame(node_rows), crs=WGS84).to_crs(LV95)

    track_rows = [
        {
            "osm_id": e["id"],
            "sport": e["tags"].get("sport"),
            "surface": e["tags"].get("surface"),
            "access": e["tags"].get("access"),
            "name": e["tags"].get("name"),
            "geometry": LineString([(p["lon"], p["lat"]) for p in e["geometry"]]),
        }
        for e in elements
        if e["type"] == "way"
    ]
    tracks = gpd.GeoDataFrame(pd.DataFrame(track_rows), crs=WGS84).to_crs(LV95)

    region.dir.mkdir(parents=True, exist_ok=True)
    stops.to_parquet(region.dir / "osm_stops.parquet")
    tracks.to_parquet(region.dir / "osm_tracks.parquet")
    return stops, tracks
