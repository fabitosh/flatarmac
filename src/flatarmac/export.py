"""Export scored network as GeoJSON for the web map."""

import geopandas as gpd
import shapely

from flatarmac.config import WGS84, Region
from flatarmac.costs import MAIN, SurfaceMode

MIN_NODE_S = 2.0


def main_block_s(edges: gpd.GeoDataFrame, mode: SurfaceMode):
    w = MAIN
    return (
        w.gradient * edges["gradient_s"]
        + w.surface * edges[f"surface_s_{mode}"]
        + w.traffic * edges["traffic_s"]
        + w.obstacles * edges["obstacle_s"]
        + w.stops * edges["stops_s"]
    )


def export(region: Region) -> None:
    edges = gpd.read_parquet(region.dir / "edges.parquet")
    nodes = gpd.read_parquet(region.dir / "nodes.parquet")
    km = edges["length_m"] / 1000

    out = edges[["edge_id", "name", "objektart", "belagsart", "kunstbaute", "road_class"]].copy()
    out["length_m"] = edges["length_m"].round(1)
    out["ascent_m"] = edges["ascent_m"].round(1)
    out["max_grade_pct"] = (edges["max_abs_grade"] * 100).round(1)
    for column in ["gradient_s", "traffic_s", "obstacle_s", "stops_s"]:
        out[column] = edges[column].round(1)
    for mode in SurfaceMode:
        out[f"surface_s_{mode}"] = edges[f"surface_s_{mode}"].round(1)
        out[f"s_per_km_{mode}"] = (main_block_s(edges, mode) / km).round(1)
    out = gpd.GeoDataFrame(out, geometry=shapely.force_2d(edges.geometry.values), crs=edges.crs)

    junctions = nodes[(nodes["through_s"] >= MIN_NODE_S) | (nodes["control"] == "signal")]
    junctions = junctions[["node_id", "degree", "control", "through_s", "geometry"]]

    web_dir = region.dir / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    out.to_crs(WGS84).to_file(web_dir / "edges.geojson", driver="GeoJSON", COORDINATE_PRECISION=6)
    junctions.to_crs(WGS84).to_file(
        web_dir / "junctions.geojson", driver="GeoJSON", COORDINATE_PRECISION=6
    )
