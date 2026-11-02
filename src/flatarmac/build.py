"""Build the scored network for a region from the fetched sources."""

import geopandas as gpd
import pandas as pd

from flatarmac import candidates, elevation, network, stops, strokes
from flatarmac.config import Region
from flatarmac.costs import STAIRS, CostModel, SurfaceMode, road_class

STRAIGHT_DEG = 60.0


def score_edges(edges: gpd.GeoDataFrame, osm_stops: gpd.GeoDataFrame, model: CostModel):
    edges = edges.copy()
    edges["road_class"] = [
        road_class(o, v) for o, v in zip(edges["objektart"], edges["verkehrsbd"], strict=True)
    ]

    profiles = [elevation.profile(g) for g in edges.geometry]
    summaries = pd.DataFrame([elevation.summarise(g) for g in edges.geometry], index=edges.index)
    edges = edges.join(summaries)
    edges["gradient_s"] = [model.gradient.edge_seconds(p) for p in profiles]

    km = edges["length_m"] / 1000
    keys = [
        model.surface_key(b, c)
        for b, c in zip(edges["belagsart"], edges["road_class"], strict=True)
    ]
    for mode in SurfaceMode:
        edges[f"surface_s_{mode}"] = km * pd.Series(keys, index=edges.index).map(
            model.surface_s_per_km[mode]
        )
    edges["traffic_s"] = km * edges["road_class"].map(model.traffic_s_per_km)
    edges["obstacle_s"] = edges["kunstbaute"].isin(STAIRS) * model.stairs_s
    edges["stops_s"] = stops.along_stops(edges, osm_stops, model)
    return edges


def through_cost(turns: pd.DataFrame) -> pd.Series:
    """Typical cost of passing a node: cheapest straight-ish movement, else cheapest non-U-turn."""
    moves = turns[turns["in_edge"] != turns["out_edge"]]
    straight = moves[moves["deflection_deg"] <= STRAIGHT_DEG].groupby("node_id")["turn_s"].min()
    any_move = moves.groupby("node_id")["turn_s"].min()
    return straight.combine_first(any_move)


def build(region: Region, model: CostModel | None = None) -> None:
    model = model or CostModel()
    roads = gpd.read_parquet(region.dir / "tlm_roads.parquet")
    osm_stops = gpd.read_parquet(region.dir / "osm_stops.parquet")

    edges, nodes = network.build(roads)
    edges = score_edges(edges, osm_stops, model)

    nodes["control"] = stops.node_controls(nodes, osm_stops)
    turns = stops.turn_table(edges, nodes["control"], model)
    nodes["through_s"] = through_cost(turns).reindex(nodes["node_id"]).fillna(0.0).to_numpy()

    stroke_table = candidates.stroke_costs(strokes.build(edges, turns), edges, turns)
    track_table = candidates.tracks(
        gpd.read_parquet(region.dir / "osm_tracks.parquet"), nodes, region.dir / "dem_2m.tif", model
    )

    edges.to_parquet(region.dir / "edges.parquet")
    nodes.to_parquet(region.dir / "nodes.parquet")
    turns.to_parquet(region.dir / "turns.parquet")
    stroke_table.to_parquet(region.dir / "strokes.parquet")
    track_table.to_parquet(region.dir / "tracks.parquet")
