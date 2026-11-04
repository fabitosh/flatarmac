"""Build the scored network for a region from the fetched sources."""

import geopandas as gpd
import pandas as pd

from flatarmac import candidates, elevation, network, roadside, stops, strokes, tracklist
from flatarmac.config import Region
from flatarmac.costs import STAIRS, CostModel, SurfaceMode, TimeOfDay, road_class

STRAIGHT_DEG = 60.0


def traffic_s(edges: pd.DataFrame, model: CostModel, time: TimeOfDay) -> pd.Series:
    """Seconds for running beside traffic: noise, no room to run, crowds (needs roadside columns)."""
    side = model.roadside
    class_rate = edges["road_class"].map(model.traffic_s_per_km)
    s_per_km = (
        side.noise_s_per_km(edges[f"noise_db_{time}"])
        + side.space_s_per_km(edges["sidewalk"], edges[f"vph_{time}"], class_rate)
        + side.crowd_s_per_km_at(edges["crowd"], time)
    )
    return edges["length_m"] / 1000 * s_per_km


def score_edges(edges: gpd.GeoDataFrame, osm_stops: gpd.GeoDataFrame, model: CostModel):
    """Per-edge penalties. Needs `road_class` and the roadside attributes."""
    edges = edges.copy()
    summaries = pd.DataFrame([elevation.summarise(g) for g in edges.geometry], index=edges.index)
    edges = edges.join(summaries)

    km = edges["length_m"] / 1000
    keys = [
        model.surface_key(b, c)
        for b, c in zip(edges["belagsart"], edges["road_class"], strict=True)
    ]
    for mode in SurfaceMode:
        edges[f"surface_s_{mode}"] = km * pd.Series(keys, index=edges.index).map(
            model.surface_s_per_km[mode]
        )
    for time in TimeOfDay:
        edges[f"traffic_s_{time}"] = traffic_s(edges, model, time)
    edges["obstacle_s"] = edges["kunstbaute"].isin(STAIRS) * model.stairs_s
    edges["stops_s"] = stops.along_stops(edges, osm_stops, model)
    return edges


def gradient_s(edges: gpd.GeoDataFrame, stroke_rows: pd.DataFrame, model: CostModel) -> pd.Series:
    """Gradient penalty per edge, graded along its stroke so ramps at junctions average out."""
    geometry = edges.set_index("edge_id").geometry
    seconds = {}
    for _, stroke in stroke_rows.sort_values(["stroke_id", "seq"]).groupby("stroke_id"):
        lines = [
            geometry[e] if forward else geometry[e].reverse()
            for e, forward in zip(stroke["edge_id"], stroke["forward"], strict=True)
        ]
        for e, p in zip(stroke["edge_id"], elevation.chain_profiles(lines), strict=True):
            seconds[e] = model.gradient.edge_seconds(p)
    return edges["edge_id"].map(seconds)


def through_cost(turns: pd.DataFrame, column: str) -> pd.Series:
    """Typical cost of passing a node: cheapest straight-ish movement, else cheapest non-U-turn."""
    moves = turns[turns["in_edge"] != turns["out_edge"]]
    straight = moves[moves["deflection_deg"] <= STRAIGHT_DEG].groupby("node_id")[column].min()
    any_move = moves.groupby("node_id")[column].min()
    return straight.combine_first(any_move)


def closed_roads(region: Region) -> set[str]:
    """TLM uuids from `curated/closures_<region>.csv`: roads mapped as open that are not."""
    path = tracklist.CURATED_DIR / f"closures_{region.name}.csv"
    return set(pd.read_csv(path)["tlm_uuid"]) if path.exists() else set()


def build(region: Region, model: CostModel | None = None) -> None:
    model = model or CostModel()
    roads = gpd.read_parquet(region.dir / "tlm_roads.parquet")
    roads = roads[~roads["uuid"].isin(closed_roads(region))]
    osm_stops = gpd.read_parquet(region.dir / "osm_stops.parquet")

    edges, nodes = network.build(roads)
    edges["road_class"] = [
        road_class(o, v) for o, v in zip(edges["objektart"], edges["verkehrsbd"], strict=True)
    ]
    noise = {
        TimeOfDay.QUIET: region.dir / "noise_night.tif",
        TimeOfDay.BUSY: region.dir / "noise_day.tif",
    }
    edges = edges.join(
        roadside.attributes(
            edges,
            gpd.read_parquet(region.dir / "osm_walk.parquet"),
            gpd.read_parquet(region.dir / "traffic_links.parquet"),
            noise,
        )
    )
    edges = score_edges(edges, osm_stops, model)

    nodes["control"] = stops.node_controls(nodes, osm_stops)
    turns = stops.turn_table(edges, nodes["control"], model)
    for time in TimeOfDay:
        through = through_cost(turns, f"turn_s_{time}").reindex(nodes["node_id"]).fillna(0.0)
        nodes[f"through_s_{time}"] = through.to_numpy()

    stroke_rows = strokes.build(edges, turns)
    edges["gradient_s"] = gradient_s(edges, stroke_rows, model)
    stroke_table = candidates.stroke_costs(stroke_rows, edges, turns)
    track_table = candidates.tracks(
        tracklist.included(region), nodes, region.dir / "dem_2m.tif", model
    )

    edges.to_parquet(region.dir / "edges.parquet")
    nodes.to_parquet(region.dir / "nodes.parquet")
    turns.to_parquet(region.dir / "turns.parquet")
    stroke_table.to_parquet(region.dir / "strokes.parquet")
    track_table.to_parquet(region.dir / "tracks.parquet")
