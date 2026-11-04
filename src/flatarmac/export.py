"""Export scored network as GeoJSON for the web map."""

import geopandas as gpd
import shapely

from flatarmac.config import WGS84, Region
from flatarmac.costs import MAIN, SurfaceMode, TimeOfDay, edge_penalty_s, score_0_100

MIN_NODE_S = 2.0


def main_block_s(edges: gpd.GeoDataFrame, mode: SurfaceMode, time: TimeOfDay):
    """Length-proportional penalties only. Point stops are shown separately: spread over a short
    edge they would dominate its per-km value."""
    return edge_penalty_s(edges, mode, MAIN, time, include_stops=False)


def export(region: Region) -> None:
    edges = gpd.read_parquet(region.dir / "edges.parquet")
    nodes = gpd.read_parquet(region.dir / "nodes.parquet")
    km = edges["length_m"] / 1000

    out = edges[["edge_id", "name", "objektart", "belagsart", "kunstbaute", "road_class"]].copy()
    out["length_m"] = edges["length_m"].round(1)
    out["ascent_m"] = edges["ascent_m"].round(1)
    out["max_grade_pct"] = (edges["max_abs_grade"] * 100).round(1)
    # Along-the-way penalties per km (comparable across piece lengths); point costs in seconds.
    out["gradient_s_per_km"] = (edges["gradient_s"] / km).round(1)
    for column in ["obstacle_s", "stops_s"]:
        out[column] = edges[column].round(1)
    # What lies beside the road, and what it costs per time of day.
    out["sidewalk"] = edges["sidewalk"]
    out["crowd"] = edges["crowd"].round(2)
    for time in TimeOfDay:
        out[f"noise_db_{time}"] = edges[f"noise_db_{time}"].round()
        out[f"vph_{time}"] = edges[f"vph_{time}"].round()
        out[f"traffic_s_per_km_{time}"] = (edges[f"traffic_s_{time}"] / km).round(1)
    for mode in SurfaceMode:
        out[f"surface_s_per_km_{mode}"] = (edges[f"surface_s_{mode}"] / km).round(1)
        for time in TimeOfDay:
            s_per_km = main_block_s(edges, mode, time) / km
            out[f"score_{mode}_{time}"] = score_0_100(s_per_km).round()
    out = gpd.GeoDataFrame(out, geometry=shapely.force_2d(edges.geometry.values), crs=edges.crs)

    through = [f"through_s_{time}" for time in TimeOfDay]
    costly = (nodes[through] >= MIN_NODE_S).any(axis=1)
    junctions = nodes[costly | (nodes["control"] == "signal")]
    junctions = junctions[["node_id", "degree", "control", *through, "geometry"]]

    web_dir = region.dir / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    out.to_crs(WGS84).to_file(web_dir / "edges.geojson", driver="GeoJSON", COORDINATE_PRECISION=6)
    junctions.to_crs(WGS84).to_file(
        web_dir / "junctions.geojson", driver="GeoJSON", COORDINATE_PRECISION=6
    )

    tracks = gpd.read_parquet(region.dir / "tracks.parquet")
    tracks = tracks[["name", "lap_m", "surface", "osm_type", "osm_id", "geometry"]]
    tracks = tracks.set_geometry(shapely.force_2d(tracks.geometry.values))
    tracks.to_crs(WGS84).to_file(
        web_dir / "tracks.geojson", driver="GeoJSON", COORDINATE_PRECISION=6
    )
