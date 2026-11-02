"""Main-block candidates prepared offline: strokes with per-mode costs, and OSM track loops."""

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio

from flatarmac import elevation
from flatarmac.costs import MAIN, CostModel, SurfaceMode, edge_penalty_s

TRACK_ACCESS_M = 100.0

# OSM surface -> surface key used by CostModel.surface_s_per_km
TRACK_SURFACE = {
    "tartan": "Hart",
    "rubber": "Hart",
    "asphalt": "Hart",
    "concrete": "Hart",
    "paved": "Hart",
    "woodchips": "Natur",
    "clay": "Natur",
    "fine_gravel": "Natur",
    "compacted": "Natur",
    "gravel": "Natur",
    "wood": "Natur",
    "ground": "Natur_path",
    "dirt": "Natur_path",
    "grass": "Natur_path",
}


def stroke_costs(strokes: pd.DataFrame, edges: pd.DataFrame, turns: pd.DataFrame) -> pd.DataFrame:
    """Per stroke position: edge length, main-block penalty per mode, turn cost into the next edge."""
    per_edge = edges[["edge_id", "length_m"]].copy()
    for mode in SurfaceMode:
        per_edge[f"cost_{mode}"] = edge_penalty_s(edges, mode, MAIN)
    out = strokes.sort_values(["stroke_id", "seq"]).merge(per_edge, on="edge_id", how="left")

    next_edge = out.groupby("stroke_id")["edge_id"].shift(-1)
    first_edge = out.groupby("stroke_id")["edge_id"].transform("first")
    next_edge = next_edge.fillna(first_edge.where(out["closed"]))
    lookup = dict(
        zip(
            zip(turns["node_id"], turns["in_edge"], turns["out_edge"], strict=True),
            turns["turn_s"],
            strict=True,
        )
    )
    # NaN marks the end of an open stroke; turns missing from the table (self-loops) are free.
    out["turn_next_s"] = [
        np.nan if pd.isna(nxt) else lookup.get((node, edge, int(nxt)), 0.0)
        for node, edge, nxt in zip(out["to_node"], out["edge_id"], next_edge, strict=True)
    ]
    return out


def tracks(
    curated: gpd.GeoDataFrame, nodes: gpd.GeoDataFrame, dem_path, model: CostModel
) -> gpd.GeoDataFrame:
    """Curated tracks (see tracklist) with per-lap cost per mode and a network access node.

    `lap_m` comes from the curated list; the ring geometry only gives shape and gradient.
    """
    rings = curated.copy()
    with rasterio.open(dem_path) as dem:
        rings["geometry"] = [elevation.drape(ring, dem) for ring in rings.geometry]
    gradient_s = np.array(
        [model.gradient.edge_seconds(elevation.profile(ring)) for ring in rings.geometry]
    )
    lap_share = rings["lap_m"].to_numpy() / rings.length.to_numpy()
    km = rings["lap_m"] / 1000
    surface_key = rings["surface"].map(TRACK_SURFACE).fillna("k_W")
    for mode in SurfaceMode:
        surface_s = km * surface_key.map(model.surface_s_per_km[mode])
        rings[f"cost_{mode}"] = MAIN.gradient * gradient_s * lap_share + MAIN.surface * surface_s

    near = gpd.sjoin_nearest(
        rings[["osm_id", "geometry"]],
        nodes[["node_id", "geometry"]],
        max_distance=TRACK_ACCESS_M,
        distance_col="access_m",
    )
    near = near[~near.index.duplicated()]
    rings = rings.join(near[["node_id", "access_m"]].rename(columns={"node_id": "access_node"}))
    rings = rings.dropna(subset=["access_node"]).astype({"access_node": int})
    return rings.reset_index(drop=True)
