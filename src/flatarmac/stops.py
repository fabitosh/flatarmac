"""Attach OSM interruptions to the TLM network and derive junction (turn) costs."""

import math
from collections import defaultdict

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from flatarmac.costs import Control, CostModel, TimeOfDay

SIGNAL_RADIUS_M = 25.0
CROSSING_RADIUS_M = 15.0
ALONG_RADIUS_M = 5.0
BEARING_LOOKAHEAD_M = 10.0

MARKED = {"marked", "zebra", "uncontrolled;marked"}


def _is_signal(stops: gpd.GeoDataFrame) -> pd.Series:
    return (
        stops["highway"].fillna("").str.contains("traffic_signals")
        | (stops["crossing"] == "traffic_signals")
        | (stops["crossing:signals"] == "yes")
    )


def _nearest(points: gpd.GeoDataFrame, targets: gpd.GeoDataFrame, radius: float, key: str):
    joined = gpd.sjoin_nearest(points, targets[[key, "geometry"]], max_distance=radius)
    return joined[~joined.index.duplicated()]


def node_controls(nodes: gpd.GeoDataFrame, stops: gpd.GeoDataFrame) -> pd.Series:
    """Strongest crossing control near each junction node (degree >= 3)."""
    junctions = nodes[nodes["degree"] >= 3]
    signal = _is_signal(stops)
    control = pd.Series(pd.NA, index=nodes.index, dtype="object")

    crossings = stops[~signal & stops["crossing"].notna()]
    near = _nearest(crossings, junctions, CROSSING_RADIUS_M, "node_id")
    marked = near.groupby("node_id")["crossing"].agg(lambda c: c.isin(MARKED).any())
    control.loc[marked.index] = np.where(marked, Control.MARKED, Control.UNMARKED)

    signalized = _nearest(stops[signal], junctions, SIGNAL_RADIUS_M, "node_id")["node_id"].unique()
    control.loc[signalized] = Control.SIGNAL
    return control


def along_stops(edges: gpd.GeoDataFrame, stops: gpd.GeoDataFrame, model: CostModel) -> pd.Series:
    """Seconds of stops located on an edge (level crossings, gates)."""
    kind = stops["railway"].fillna(stops["barrier"])
    candidates = stops.assign(kind=kind)[kind.isin(model.along_stop_s.keys())]
    near = _nearest(candidates, edges, ALONG_RADIUS_M, "edge_id")
    seconds = near["kind"].map(model.along_stop_s).groupby(near["edge_id"]).sum()
    return seconds.reindex(edges["edge_id"], fill_value=0.0).to_numpy()


def _bearing(line: shapely.LineString, at_start: bool) -> float:
    """Direction pointing away from the node, in degrees."""
    length = line.length
    look = min(BEARING_LOOKAHEAD_M, length)
    origin = line.interpolate(0 if at_start else length)
    target = line.interpolate(look if at_start else length - look)
    return math.degrees(math.atan2(target.y - origin.y, target.x - origin.x)) % 360


def crossing_costs(edges: gpd.GeoDataFrame, model: CostModel) -> dict:
    """Seconds to cross each edge's road at a junction, per (control, time): by its traffic volume,
    else by its width class."""
    out = {}
    for control in Control:
        class_s = edges["road_class"].map(model.crossing_s[control]).to_numpy()
        for time in TimeOfDay:
            vph = edges[f"vph_{time}"].to_numpy()
            out[control, time] = np.array(
                [
                    model.roadside.crossing_s(control, v, c, time)
                    for v, c in zip(vph, class_s, strict=True)
                ]
            )
    return out


def turn_table(edges: gpd.GeoDataFrame, controls: pd.Series, model: CostModel) -> pd.DataFrame:
    """Cost of passing node n from in_edge to out_edge, in seconds, per time of day.

    Other edges at the junction split into the two sides of the path; the runner is assumed to be
    on the cheaper side and to cross every road on it. U-turns are included (in_edge == out_edge).
    """
    crossing = crossing_costs(edges, model)
    position = pd.Series(np.arange(len(edges)), index=edges["edge_id"])
    incident: dict[int, list[tuple[int, float, int]]] = defaultdict(list)
    for edge in edges.itertuples():
        if edge.u == edge.v:
            continue  # self-loops: rare and bearing is ambiguous
        line = edge.geometry
        row = position[edge.edge_id]
        incident[edge.u].append((edge.edge_id, _bearing(line, True), row))
        incident[edge.v].append((edge.edge_id, _bearing(line, False), row))

    times = list(TimeOfDay)
    rows = []
    for node, spokes in incident.items():
        control = controls.iloc[node] if pd.notna(controls.iloc[node]) else Control.UNMARKED
        cost = [crossing[control, time] for time in times]
        for in_id, in_bearing, _ in spokes:
            for out_id, out_bearing, _ in spokes:
                ccw = (out_bearing - in_bearing) % 360
                deflection = 180 - ccw if ccw <= 180 else ccw - 180
                side_s = [0.0] * len(times)
                if in_id == out_id:
                    deflection = 180.0
                else:
                    left = [0.0] * len(times)
                    right = [0.0] * len(times)
                    for other_id, b, row in spokes:
                        if other_id in (in_id, out_id):
                            continue
                        side = left if 0 < (b - in_bearing) % 360 < ccw else right
                        for i, c in enumerate(cost):
                            side[i] += c[row]
                    side_s = [min(lf, rt) for lf, rt in zip(left, right, strict=True)]
                angle_s = model.turn_s(deflection)
                rows.append((node, in_id, out_id, deflection, *(s + angle_s for s in side_s)))
    columns = ["node_id", "in_edge", "out_edge", "deflection_deg"]
    return pd.DataFrame(rows, columns=columns + [f"turn_s_{time}" for time in times])
