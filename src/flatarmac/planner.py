"""Route suggestions: warm-up -> flat main block (laps) -> cool-down.

The main block is repeated laps of one candidate: an out-and-back on a sub-stretch of a stroke, a
closed stroke, or a running track. Warm-up and cool-down are routed with relaxed weights. Transit up
to the planned warm-up/cool-down distance is free (any remaining warm-up happens on the flat block);
transit beyond it is penalised.
"""

from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from flatarmac.config import LV95, WGS84, Region
from flatarmac.costs import TRANSIT, CostModel, SurfaceMode, edge_penalty_s
from flatarmac.pace import Runner, Zone

MIN_STRETCH_M = 150.0
EXCESS_TRANSIT_S_PER_KM = 120.0
REACH_FACTOR = 1.6  # search radius relative to the transit budget
MIN_SEPARATION_M = 400.0


@dataclass(frozen=True)
class Request:
    start: tuple[float, float]  # (lat, lon)
    main_m: float
    warmup_m: float
    cooldown_m: float
    easy_pace_s_per_km: float
    mode: SurfaceMode = SurfaceMode.TARMAC
    end: tuple[float, float] | None = None
    top: int = 10


def make_request(
    start: tuple[float, float],
    main_value: float,
    main_unit: str,  # "km" | "m" | "min"
    warmup_min: float,
    cooldown_min: float,
    five_k: str,
    main_zone: str = "threshold",
    mode: str = "tarmac",
    end: tuple[float, float] | None = None,
    top: int = 10,
) -> Request:
    """Request from runner-facing inputs: durations become distances via 5k-derived paces."""
    runner = Runner.from_5k(five_k)
    if main_unit == "min":
        main_m = runner.distance_m(main_value * 60, Zone(main_zone))
    else:
        main_m = main_value * (1000 if main_unit == "km" else 1)
    return Request(
        start=start,
        end=end,
        main_m=main_m,
        warmup_m=runner.distance_m(warmup_min * 60, Zone.EASY),
        cooldown_m=runner.distance_m(cooldown_min * 60, Zone.EASY),
        easy_pace_s_per_km=runner.pace_s_per_km(Zone.EASY),
        mode=SurfaceMode(mode),
        top=top,
    )


@dataclass
class Transit:
    """Best relaxed-cost paths from one node to every reachable node."""

    source: int
    dist_m: np.ndarray
    penalty_s: np.ndarray
    predecessors: np.ndarray

    def path_nodes(self, target: int) -> list[int]:
        nodes = [target]
        while nodes[-1] != self.source:
            nodes.append(int(self.predecessors[nodes[-1]]))
        return nodes[::-1]  # source -> target

    def score(self, node, budget_m: float) -> np.ndarray:
        excess = np.maximum(self.dist_m[node] - budget_m, 0.0)
        return self.penalty_s[node] + EXCESS_TRANSIT_S_PER_KM * excess / 1000


@dataclass
class Suggestion:
    kind: str  # out_and_back | loop | track
    name: str
    lap_m: float
    laps: float
    main_s: float
    warmup_s: float
    cooldown_s: float
    warmup_m: float
    cooldown_m: float
    access_node: int
    main_line: shapely.LineString  # one lap (out-and-back: the stretch one way)
    warmup_nodes: list[int] = field(default_factory=list)
    cooldown_nodes: list[int] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.main_s + self.warmup_s + self.cooldown_s


class Planner:
    def __init__(self, region: Region, model: CostModel | None = None):
        self.model = model or CostModel()
        self.edges = gpd.read_parquet(region.dir / "edges.parquet")
        self.nodes = gpd.read_parquet(region.dir / "nodes.parquet")
        self.tracks = gpd.read_parquet(region.dir / "tracks.parquet")
        strokes = pd.read_parquet(region.dir / "strokes.parquet")
        self.strokes = {sid: group for sid, group in strokes.groupby("stroke_id", sort=False)}
        self.node_tree = shapely.STRtree(self.nodes.geometry.values)
        named = self.edges[self.edges["name"].notna()]
        self.named_tree = shapely.STRtree(named.geometry.values)
        self.named = named["name"].to_numpy()
        self.to_lv95 = Transformer.from_crs(WGS84, LV95, always_xy=True)
        self.edge_by_pair: dict[tuple[int, int], int] = {}

    # --- transit -------------------------------------------------------------------------------

    def _transit_graph(self, mode: SurfaceMode, easy_pace_s_per_km: float):
        e = self.edges
        through = self.nodes["through_s"].to_numpy()
        penalty = edge_penalty_s(e, mode, TRANSIT).to_numpy() + 0.5 * TRANSIT.stops * (
            through[e["u"]] + through[e["v"]]
        )
        weight = e["length_m"].to_numpy() * easy_pace_s_per_km / 1000 + penalty
        both = pd.DataFrame(
            {
                "a": np.concatenate([e["u"], e["v"]]),
                "b": np.concatenate([e["v"], e["u"]]),
                "w": np.concatenate([weight, weight]),
                "edge": np.concatenate([e["edge_id"], e["edge_id"]]),
            }
        )
        both = both[both["a"] != both["b"]].sort_values("w").drop_duplicates(["a", "b"])
        n = len(self.nodes)
        graph = csr_matrix((both["w"], (both["a"], both["b"])), shape=(n, n))
        pair_edge = dict(zip(zip(both["a"], both["b"], strict=True), both["edge"], strict=True))
        return graph, pair_edge, penalty

    def _transit(self, graph, pair_edge, penalty, source: int, limit_s: float) -> Transit:
        cost, pred = dijkstra(graph, indices=source, limit=limit_s, return_predecessors=True)
        n = len(cost)
        dist = np.full(n, np.inf)
        pen = np.full(n, np.inf)
        dist[source] = pen[source] = 0.0
        length = self.edges["length_m"].to_numpy()
        reached = np.flatnonzero(np.isfinite(cost))
        for node in reached[np.argsort(cost[reached])]:
            if node == source:
                continue
            parent = pred[node]
            edge = pair_edge[(parent, node)]
            dist[node] = dist[parent] + length[edge]
            pen[node] = pen[parent] + penalty[edge]
        return Transit(source, dist, pen, pred)

    def snap(self, lat_lon: tuple[float, float]) -> int:
        x, y = self.to_lv95.transform(lat_lon[1], lat_lon[0])
        return int(self.node_tree.nearest(shapely.Point(x, y)))

    # --- candidates ------------------------------------------------------------------------------

    def _stroke_candidates(self, req: Request, wu: Transit, cd: Transit) -> list[Suggestion]:
        cost_col = f"cost_{req.mode}"
        u_turn = self.model.turn_s(180.0)
        results = []
        for group in self.strokes.values():
            nodes = np.append(group["from_node"].to_numpy(), group["to_node"].to_numpy()[-1])
            transit = wu.score(nodes, req.warmup_m) + cd.score(nodes, req.cooldown_m)
            if not np.isfinite(transit).any():
                continue
            length = group["length_m"].to_numpy()
            cost = group[cost_col].to_numpy()
            turn = group["turn_next_s"].to_numpy()

            if group["closed"].iloc[0]:
                lap_m = length.sum()
                if lap_m < MIN_STRETCH_M:
                    continue
                lap_s = cost.sum() + np.nansum(turn)
                main_s = lap_s * req.main_m / lap_m
                access_node = int(nodes[np.argmin(transit)])
                results.append(
                    self._suggestion(
                        "loop", group, 0, len(group), lap_m, main_s, req, wu, cd, access_node
                    )
                )
                continue

            k = len(group)
            cum_len = np.concatenate([[0.0], np.cumsum(length)])
            cum_cost = np.concatenate([[0.0], np.cumsum(cost)])
            # Turn at internal node m (between edge m-1 and m), m = 1..k-1.
            turn_at = np.concatenate([[0.0], np.nan_to_num(turn[:-1]), [0.0]])
            cum_turn = np.cumsum(turn_at)

            best_score, best = np.inf, None
            for i in range(k):
                j = np.arange(i + 1, k + 1)
                stretch = cum_len[j] - cum_len[i]
                internal_turns = cum_turn[j - 1] - cum_turn[i]
                one_way = cum_cost[j] - cum_cost[i] + internal_turns
                lap_m = 2 * stretch
                lap_s = 2 * one_way + 2 * u_turn
                main_s = lap_s * req.main_m / lap_m
                # Best access anywhere on the stretch: running min of transit over nodes i..j.
                access = np.minimum.accumulate(transit[i:])[1:]
                ok = (stretch >= MIN_STRETCH_M) & (stretch <= req.main_m / 2 * 1.05)
                score = np.where(ok, main_s + access, np.inf)
                m = int(np.argmin(score))
                if score[m] < best_score:
                    best_score, best = score[m], (i, int(j[m]), lap_m[m], main_s[m])
            if best is None:
                continue
            i, j, lap_m, main_s = best
            access_node = int(nodes[i + np.argmin(transit[i : j + 1])])
            results.append(
                self._suggestion(
                    "out_and_back", group, i, j, lap_m, main_s, req, wu, cd, access_node
                )
            )
        return results

    def _track_candidates(self, req: Request, wu: Transit, cd: Transit) -> list[Suggestion]:
        results = []
        for track in self.tracks.itertuples():
            node = track.access_node
            if not np.isfinite(wu.dist_m[node]) or not np.isfinite(cd.dist_m[node]):
                continue
            lap_s = getattr(track, f"cost_{req.mode}")
            results.append(
                Suggestion(
                    kind="track",
                    name=track.name,
                    lap_m=track.lap_m,
                    laps=req.main_m / track.lap_m,
                    main_s=lap_s * req.main_m / track.lap_m,
                    warmup_s=float(wu.score(node, req.warmup_m - track.access_m)),
                    cooldown_s=float(cd.score(node, req.cooldown_m - track.access_m)),
                    warmup_m=float(wu.dist_m[node] + track.access_m),
                    cooldown_m=float(cd.dist_m[node] + track.access_m),
                    access_node=node,
                    main_line=track.geometry,
                )
            )
        return results

    def _suggestion(self, kind, group, i, j, lap_m, main_s, req, wu, cd, access_node):
        rows = group.iloc[i:j]
        parts = []
        for edge_id, forward in zip(rows["edge_id"], rows["forward"], strict=True):
            coords = shapely.get_coordinates(self.edges.geometry.iloc[edge_id], include_z=True)
            parts.append(coords if forward else coords[::-1])
        line = shapely.LineString(np.vstack(parts))
        names = self.edges["name"].iloc[rows["edge_id"]].dropna()
        name = names.mode().iloc[0] if len(names) else self._nearby_name(line)
        return Suggestion(
            kind=kind,
            name=name,
            lap_m=float(lap_m),
            laps=req.main_m / lap_m,
            main_s=float(main_s),
            warmup_s=float(wu.score(access_node, req.warmup_m)),
            cooldown_s=float(cd.score(access_node, req.cooldown_m)),
            warmup_m=float(wu.dist_m[access_node]),
            cooldown_m=float(cd.dist_m[access_node]),
            access_node=access_node,
            main_line=line,
        )

    def _nearby_name(self, line: shapely.LineString) -> str:
        index = self.named_tree.nearest(line.centroid)
        return f"path near {self.named[index]}"

    # --- entry point -----------------------------------------------------------------------------

    def plan(self, req: Request) -> list[Suggestion]:
        graph, pair_edge, penalty = self._transit_graph(req.mode, req.easy_pace_s_per_km)
        self.edge_by_pair = pair_edge
        reach_m = REACH_FACTOR * max(req.warmup_m, req.cooldown_m) + 1000
        limit_s = reach_m * req.easy_pace_s_per_km / 1000 * 1.5

        start = self.snap(req.start)
        wu = self._transit(graph, pair_edge, penalty, start, limit_s)
        if req.end is None:
            cd = wu
        else:
            cd = self._transit(graph, pair_edge, penalty, self.snap(req.end), limit_s)

        candidates = self._stroke_candidates(req, wu, cd) + self._track_candidates(req, wu, cd)
        candidates.sort(key=lambda s: s.score)

        picked: list[Suggestion] = []
        for candidate in candidates:
            centre = candidate.main_line.centroid
            if all(centre.distance(p.main_line) >= MIN_SEPARATION_M for p in picked):
                picked.append(candidate)
            if len(picked) == req.top:
                break
        for suggestion in picked:
            suggestion.warmup_nodes = wu.path_nodes(suggestion.access_node)
            suggestion.cooldown_nodes = cd.path_nodes(suggestion.access_node)[::-1]
        return picked
