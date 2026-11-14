"""Map layers for the web UI, built from the region's tables when the server starts.

The edge layer carries only what the map draws (scores per surface mode and time of day); the
details shown on hover come from `edge_details` one edge at a time.
"""

import geopandas as gpd
import pandas as pd
import shapely

from flatarmac.config import WGS84
from flatarmac.costs import MAIN, SurfaceMode, TimeOfDay, edge_penalty_s, score_0_100

MIN_NODE_S = 2.0


def main_block_s(edges: gpd.GeoDataFrame, mode: SurfaceMode, time: TimeOfDay):
    """Length-proportional penalties only. Point stops are shown separately: spread over a short
    edge they would dominate its per-km value."""
    return edge_penalty_s(edges, mode, MAIN, time, include_stops=False)


def edge_scores(edges: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Edge id and 0-100 runnability score per surface mode and time of day."""
    km = edges["length_m"] / 1000
    out = edges[["edge_id"]].copy()
    for mode in SurfaceMode:
        for time in TimeOfDay:
            s_per_km = main_block_s(edges, mode, time) / km
            out[f"score_{mode}_{time}"] = score_0_100(s_per_km).round().astype(int)
    return gpd.GeoDataFrame(out, geometry=edges.geometry.values, crs=edges.crs)


def edge_details(edges: gpd.GeoDataFrame) -> pd.DataFrame:
    """What the hover popup shows: penalties per km (comparable across piece lengths), point
    costs in seconds, and what lies beside the road per time of day."""
    km = edges["length_m"] / 1000
    out = edges[["edge_id", "name", "objektart", "belagsart", "kunstbaute", "road_class"]].copy()
    out["length_m"] = edges["length_m"].round(1)
    out["ascent_m"] = edges["ascent_m"].round(1)
    out["max_grade_pct"] = (edges["max_abs_grade"] * 100).round(1)
    out["gradient_s_per_km"] = (edges["gradient_s"] / km).round(1)
    for column in ["obstacle_s", "stops_s"]:
        out[column] = edges[column].round(1)
    out["sidewalk"] = edges["sidewalk"]
    out["crowd"] = edges["crowd"].round(2)
    for time in TimeOfDay:
        out[f"noise_db_{time}"] = edges[f"noise_db_{time}"].round()
        out[f"vph_{time}"] = edges[f"vph_{time}"].round()
        out[f"traffic_s_per_km_{time}"] = (edges[f"traffic_s_{time}"] / km).round(1)
    for mode in SurfaceMode:
        out[f"surface_s_per_km_{mode}"] = (edges[f"surface_s_{mode}"] / km).round(1)
    return pd.DataFrame(out)


def junctions(nodes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Signals and junctions that cost at least MIN_NODE_S to pass at some time of day."""
    through = [f"through_s_{time}" for time in TimeOfDay]
    costly = (nodes[through] >= MIN_NODE_S).any(axis=1)
    return nodes[costly | (nodes["control"] == "signal")][
        ["node_id", "degree", "control", *through, "geometry"]
    ]


def tracks(tracks: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    return tracks[["name", "lap_m", "surface", "osm_type", "osm_id", "geometry"]]


def to_geojson(layer: gpd.GeoDataFrame) -> bytes:
    """Compact GeoJSON in WGS84 with coordinates to 6 decimals (~0.1 m)."""
    geometry = shapely.force_2d(layer.geometry.values)
    geometry = gpd.GeoSeries(geometry, crs=layer.crs).to_crs(WGS84).values
    shapes = shapely.to_geojson(shapely.transform(geometry, lambda xy: xy.round(6)))
    properties = pd.DataFrame(layer.drop(columns=layer.geometry.name))
    rows = properties.to_json(orient="records", lines=True).splitlines() if len(layer) else []
    features = ",".join(
        f'{{"type":"Feature","properties":{p},"geometry":{g}}}'
        for p, g in zip(rows, shapes, strict=True)
    )
    return f'{{"type":"FeatureCollection","features":[{features}]}}'.encode()
