"""Route suggestions: warm-up -> flat main block -> cool-down.

The main block is laps of one "block": an out-and-back on part of a stroke, a loop (closed stroke
or a cycle in the network), or a running track. Warm-up and cool-down are routed with relaxed
weights. If the block is further away than the warm-up (cool-down) covers, the extra distance is
run as part of the main block, scored with main-block weights, and fewer laps remain on the block.
If it is closer, the rest of the warm-up (cool-down) is run on the block with relaxed weights.
"""

import itertools
from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from flatarmac.config import LV95, WGS84, Region
from flatarmac.costs import MAIN, TRANSIT, CostModel, SurfaceMode, edge_penalty_s
from flatarmac.pace import Runner, Zone, flat_equivalent_m

MIN_STRETCH_M = 150.0
MIN_LOOP_M = 400.0
MAX_LOOP_M = 6000.0
MAX_ON_THE_WAY = 0.5  # at most this share of the main block is run on the way to / from the block
REACH_FACTOR = 1.6  # search radius relative to the warm-up / cool-down distance
MIN_SEPARATION_M = 400.0
LAP_BANDS = (1000.0, 2500.0)  # lap length bands: short < 1 km <= medium < 2.5 km <= long


def lap_band(lap_m: float) -> str:
    if lap_m < LAP_BANDS[0]:
        return "short"
    return "medium" if lap_m < LAP_BANDS[1] else "long"


@dataclass(frozen=True)
class Request:
    """Distances in metres; warm-up / cool-down are flat-equivalent (minutes at easy pace)."""

    start: tuple[float, float]  # (lat, lon)
    main_m: float
    warmup_m: float
    cooldown_m: float
    easy_pace_s_per_km: float
    mode: SurfaceMode = SurfaceMode.TARMAC
    end: tuple[float, float] | None = None
    top: int = 10

    @property
    def total_m(self) -> float:
        return self.warmup_m + self.main_m + self.cooldown_m


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
    """Best relaxed-cost paths from one node to every reachable node.

    Warm-up / cool-down budgets are flat-equivalent metres (minutes at easy pace), compared with
    `flat_m`, the grade-adjusted distance along the path. `legs[budget]` is the penalty of the path
    to each node when the first `budget` flat-equivalent metres are warm-up / cool-down (relaxed
    weights) and anything beyond is main block (strict weights).
    """

    source: int
    dist_m: np.ndarray
    flat_m: np.ndarray
    legs: dict[float, np.ndarray]
    predecessors: np.ndarray

    def path_nodes(self, target: int) -> list[int]:
        nodes = [target]
        while nodes[-1] != self.source:
            nodes.append(int(self.predecessors[nodes[-1]]))
        return nodes[::-1]  # source -> target

    def excess_m(self, node, budget_m: float):
        """Metres of the path beyond the budget (flat-equivalent excess scaled back to ground)."""
        flat = self.flat_m[node]
        ratio = np.divide(
            self.dist_m[node], flat, out=np.ones_like(flat), where=np.isfinite(flat) & (flat > 0)
        )
        return np.maximum(flat - budget_m, 0.0) * ratio

    def unused_m(self, node, budget_m: float):
        """Budget left over on arrival, run on the block instead (flat, so no conversion)."""
        return np.maximum(budget_m - self.flat_m[node], 0.0)

    def leg_s(self, node, budget_m: float):
        return self.legs[budget_m][node]


@dataclass
class Block:
    """A place to run laps."""

    kind: str  # out_and_back | loop | track
    lap_m: float
    lap_s: float  # main-block penalty per lap
    turn_s: float  # part of lap_s from turns and junctions
    nodes: np.ndarray  # network nodes where the block can be joined
    edges: list[tuple[int, bool]] = field(default_factory=list)  # (edge_id, forward), one way
    geometry: shapely.LineString | None = None  # tracks only
    access_m: float = 0.0  # tracks: connector from the network
    name: str | None = None
    easy_s: float = 0.0  # penalty per lap with warm-up / cool-down weights

    @property
    def band(self) -> str:
        return lap_band(self.lap_m)


@dataclass
class Suggestion:
    kind: str
    name: str
    lap_m: float
    laps: float
    main_s: float  # main-block penalty: laps + parts run on the way
    warmup_s: float  # incl. the part of the warm-up run on the block
    cooldown_s: float
    warmup_m: float  # distance from start to the block
    cooldown_m: float  # distance from the block to the end
    approach_m: float  # part of the way to the block run as main block
    return_m: float  # part of the way back run as main block
    easy_on_block_m: float  # warm-up / cool-down the way there and back doesn't cover
    access_node: int
    main_line: shapely.LineString  # one lap (out-and-back: the stretch one way)
    warmup_nodes: list[int] = field(default_factory=list)
    cooldown_nodes: list[int] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.main_s + self.warmup_s + self.cooldown_s

    @property
    def band(self) -> str:
        return lap_band(self.lap_m)


class Planner:
    def __init__(self, region: Region, model: CostModel | None = None):
        self.model = model or CostModel()
        self.edges = gpd.read_parquet(region.dir / "edges.parquet")
        self.nodes = gpd.read_parquet(region.dir / "nodes.parquet")
        self.tracks = gpd.read_parquet(region.dir / "tracks.parquet")
        strokes = pd.read_parquet(region.dir / "strokes.parquet")
        self.strokes = {sid: group for sid, group in strokes.groupby("stroke_id", sort=False)}
        turns = pd.read_parquet(region.dir / "turns.parquet")
        self.turn_s = dict(
            zip(
                zip(turns["node_id"], turns["in_edge"], turns["out_edge"], strict=True),
                turns["turn_s"],
                strict=True,
            )
        )
        self.node_xy = shapely.get_coordinates(self.nodes.geometry.values)
        self.node_tree = shapely.STRtree(self.nodes.geometry.values)
        named = self.edges[self.edges["name"].notna()]
        self.named_tree = shapely.STRtree(named.geometry.values)
        self.named = named["name"].to_numpy()
        self.to_lv95 = Transformer.from_crs(WGS84, LV95, always_xy=True)
        self.edge_by_pair: dict[tuple[int, int], int] = {}
        e = self.edges
        self.edge_u = e["u"].to_numpy()
        # Flat-equivalent length per direction: u -> v and v -> u.
        self.flat_fwd = flat_equivalent_m(e["length_m"], e["ascent_m"], e["descent_m"]).to_numpy()
        self.flat_bwd = flat_equivalent_m(e["length_m"], e["descent_m"], e["ascent_m"]).to_numpy()

    # --- graphs ----------------------------------------------------------------------------------

    def _graph(self, penalty: np.ndarray, easy_pace_s_per_km: float):
        """Undirected graph (as symmetric CSR) weighted by easy-pace time + penalty."""
        e = self.edges
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
        return graph, pair_edge

    def _penalties(self, mode: SurfaceMode, weights) -> np.ndarray:
        """Per-edge penalty incl. half the junction cost at each end."""
        through = self.nodes["through_s"].to_numpy()
        e = self.edges
        return edge_penalty_s(e, mode, weights).to_numpy() + 0.5 * weights.stops * (
            through[e["u"]] + through[e["v"]]
        )

    def _tree(self, graph, pair_edge, source: int, limit_s: float):
        """Dijkstra tree: predecessors, distance along tree paths, nodes in settle order."""
        cost, pred = dijkstra(graph, indices=source, limit=limit_s, return_predecessors=True)
        length = self.edges["length_m"].to_numpy()
        dist = np.full(len(cost), np.inf)
        dist[source] = 0.0
        reached = np.flatnonzero(np.isfinite(cost))
        order = reached[np.argsort(cost[reached])]
        for node in order[1:]:
            dist[node] = dist[pred[node]] + length[pair_edge[(pred[node], node)]]
        return pred, dist, order

    def _transit(self, graph, pair_edge, source, limit_s, relaxed, strict, budgets) -> Transit:
        """Tree from `source`; per budget, penalty with relaxed weights for the first `budget`
        flat-equivalent metres and strict weights beyond (edges straddling the budget are split
        pro rata)."""
        pred, dist, order = self._tree(graph, pair_edge, source, limit_s)
        flat = np.full(len(dist), np.inf)
        flat[source] = 0.0
        step = np.zeros(len(dist))  # flat-equivalent length of the tree edge into each node
        for node in order[1:]:
            parent = pred[node]
            edge = pair_edge[(parent, node)]
            step[node] = (self.flat_fwd if self.edge_u[edge] == parent else self.flat_bwd)[edge]
            flat[node] = flat[parent] + step[node]
        legs = {}
        for budget in set(budgets):
            leg = np.full(len(dist), np.inf)
            leg[source] = 0.0
            for node in order[1:]:
                parent = pred[node]
                edge = pair_edge[(parent, node)]
                beyond = np.clip((flat[node] - budget) / max(step[node], 1e-9), 0.0, 1.0)
                leg[node] = leg[parent] + (1 - beyond) * relaxed[edge] + beyond * strict[edge]
            legs[budget] = leg
        return Transit(source, dist, flat, legs, pred)

    def snap(self, lat_lon: tuple[float, float]) -> int:
        x, y = self.to_lv95.transform(lat_lon[1], lat_lon[0])
        return int(self.node_tree.nearest(shapely.Point(x, y)))

    # --- blocks ----------------------------------------------------------------------------------

    def _stroke_blocks(self, req: Request, transit_s: np.ndarray) -> list[Block]:
        """Out-and-backs: per stroke and lap band, the best section (incl. how to get there).
        Closed strokes become loops."""
        cost_col = f"cost_{req.mode}"
        u_turn = self.model.turn_s(180.0)
        blocks = []
        for group in self.strokes.values():
            nodes = np.append(group["from_node"].to_numpy(), group["to_node"].to_numpy()[-1])
            transit = transit_s[nodes]
            if not np.isfinite(transit).any():
                continue
            length = group["length_m"].to_numpy()
            cost = group[cost_col].to_numpy()
            turn = group["turn_next_s"].to_numpy()
            edges = list(zip(group["edge_id"], group["forward"], strict=True))

            if group["closed"].iloc[0]:
                if length.sum() >= MIN_LOOP_M:
                    turn_s = np.nansum(turn)
                    blocks.append(
                        Block("loop", length.sum(), cost.sum() + turn_s, turn_s, nodes, edges)
                    )
                continue

            k = len(group)
            cum_len = np.concatenate([[0.0], np.cumsum(length)])
            cum_cost = np.concatenate([[0.0], np.cumsum(cost)])
            # Turn at internal node m (between edge m-1 and m), m = 1..k-1.
            turn_at = np.concatenate([[0.0], np.nan_to_num(turn[:-1]), [0.0]])
            cum_turn = np.cumsum(turn_at)

            best: dict[int, tuple[float, int, int, float, float, float]] = {}
            for i in range(k):
                j = np.arange(i + 1, k + 1)
                stretch = cum_len[j] - cum_len[i]
                one_way_turn = cum_turn[j - 1] - cum_turn[i]
                lap_m = 2 * stretch
                lap_turn = 2 * one_way_turn + 2 * u_turn
                lap_s = 2 * (cum_cost[j] - cum_cost[i]) + lap_turn
                # Best access anywhere on the section: running min of transit over nodes i..j.
                access = np.minimum.accumulate(transit[i:])[1:]
                score = lap_s * req.main_m / lap_m + access
                ok = (stretch >= MIN_STRETCH_M) & (stretch <= req.main_m / 2 * 1.05)
                band_of = np.digitize(lap_m, LAP_BANDS)
                for band in np.unique(band_of[ok]):
                    m = int(np.argmin(np.where(ok & (band_of == band), score, np.inf)))
                    if np.isfinite(score[m]) and score[m] < best.get(band, (np.inf,))[0]:
                        best[band] = (score[m], i, int(j[m]), lap_m[m], lap_s[m], lap_turn[m])
            for _, i, j, lap_m, lap_s, lap_turn in best.values():
                blocks.append(
                    Block("out_and_back", lap_m, lap_s, lap_turn, nodes[i : j + 1], edges[i:j])
                )
        return blocks

    def _track_blocks(self, req: Request) -> list[Block]:
        return [
            Block(
                kind="track",
                lap_m=t.lap_m,
                lap_s=getattr(t, f"cost_{req.mode}"),
                turn_s=0.0,
                nodes=np.array([t.access_node]),
                geometry=t.geometry,
                access_m=t.access_m,
                name=t.name,
            )
            for t in self.tracks.itertuples()
        ]

    def _cycle_blocks(self, req: Request, source: int, limit_s: float) -> list[Block]:
        """Loops from a shortest-path tree on main-block costs: every non-tree edge closes a cycle
        made of two good branches. One Dijkstra gives thousands of loops of all sizes."""
        penalty = self._penalties(req.mode, MAIN)
        graph, pair_edge = self._graph(penalty, req.easy_pace_s_per_km)
        pred, dist, order = self._tree(graph, pair_edge, source, limit_s)
        e = self.edges
        length = e["length_m"].to_numpy()
        main_cost = edge_penalty_s(e, req.mode, MAIN).to_numpy()
        u_arr, v_arr = e["u"].to_numpy(), e["v"].to_numpy()
        max_lap = min(MAX_LOOP_M, req.main_m)

        reached = np.isfinite(dist)
        tree_edge = np.zeros(len(e), dtype=bool)
        for node in order[1:]:
            tree_edge[pair_edge[(pred[node], node)]] = True
        closing = np.flatnonzero(reached[u_arr] & reached[v_arr] & ~tree_edge & (u_arr != v_arr))

        blocks = []
        for edge in closing:
            u, v = int(u_arr[edge]), int(v_arr[edge])
            # Climb from the farther end until both branches meet; give up past max_lap.
            left, right = [u], [v]
            total = length[edge]
            while left[-1] != right[-1] and total <= max_lap:
                side = left if dist[left[-1]] >= dist[right[-1]] else right
                parent = int(pred[side[-1]])
                if parent < 0:
                    break
                total += length[pair_edge[(parent, side[-1])]]
                side.append(parent)
            if left[-1] != right[-1] or not MIN_LOOP_M <= total <= max_lap:
                continue
            # Cycle: meet -> ... -> u, (u, v), v -> ... -> meet
            ring = left[::-1] + right
            ring_edges = [pair_edge[pair] for pair in itertools.pairwise(ring)]
            ring_edges[len(left) - 1] = int(edge)  # the closing edge, not a parallel one
            nodes = ring[:-1]
            incoming = ring_edges[-1:] + ring_edges[:-1]
            turn_s = sum(
                self.turn_s.get(key, 0.0) for key in zip(nodes, incoming, ring_edges, strict=True)
            )
            lap_s = main_cost[ring_edges].sum() + turn_s
            forward = [u_arr[x] == a for a, x in zip(nodes, ring_edges, strict=True)]
            blocks.append(
                Block(
                    "loop",
                    float(total),
                    float(lap_s),
                    float(turn_s),
                    np.array(nodes),
                    list(zip(ring_edges, forward, strict=True)),
                )
            )
        return blocks

    # --- evaluation ------------------------------------------------------------------------------

    @staticmethod
    def _easy_lap_s(block: Block, easy_edge: np.ndarray) -> float:
        """Penalty of one lap with warm-up / cool-down weights."""
        if block.kind == "track":
            return TRANSIT.surface * block.lap_s  # track cost is surface (+ negligible gradient)
        runs = 2 if block.kind == "out_and_back" else 1
        edge_ids = [e for e, _ in block.edges]
        return runs * float(easy_edge[edge_ids].sum()) + TRANSIT.stops * block.turn_s

    def _evaluate(self, block: Block, req: Request, wu: Transit, cd: Transit) -> Suggestion | None:
        nodes = block.nodes
        wu_budget = req.warmup_m - block.access_m
        cd_budget = req.cooldown_m - block.access_m
        on_the_way = wu.excess_m(nodes, wu_budget) + cd.excess_m(nodes, cd_budget)
        feasible = on_the_way <= MAX_ON_THE_WAY * req.main_m  # also False when unreachable
        laps_m = np.where(feasible, req.main_m - on_the_way, 0.0)
        # Warm-up / cool-down the way to the block doesn't cover is run on the block.
        easy_per_m = block.easy_s / block.lap_m
        wu_s = wu.leg_s(nodes, req.warmup_m) + easy_per_m * wu.unused_m(nodes, wu_budget)
        cd_s = cd.leg_s(nodes, req.cooldown_m) + easy_per_m * cd.unused_m(nodes, cd_budget)
        # Track connectors (<= 100 m) are ignored in the penalty split.
        score = np.where(feasible, block.lap_s * laps_m / block.lap_m + wu_s + cd_s, np.inf)
        if not np.isfinite(score).any():
            return None
        best = int(np.argmin(score))
        node = int(nodes[best])
        return Suggestion(
            kind=block.kind,
            name=block.name or "",
            lap_m=float(block.lap_m),
            laps=float(laps_m[best] / block.lap_m),
            main_s=float(block.lap_s * laps_m[best] / block.lap_m),
            warmup_s=float(wu_s[best]),
            cooldown_s=float(cd_s[best]),
            warmup_m=float(wu.dist_m[node] + block.access_m),
            cooldown_m=float(cd.dist_m[node] + block.access_m),
            approach_m=float(wu.excess_m(node, wu_budget)),
            return_m=float(cd.excess_m(node, cd_budget)),
            easy_on_block_m=float(wu.unused_m(node, wu_budget) + cd.unused_m(node, cd_budget)),
            access_node=node,
            main_line=block.geometry,  # filled for picked blocks
        )

    def _block_line(self, block: Block) -> shapely.LineString:
        if block.geometry is not None:
            return block.geometry
        parts = []
        for edge_id, forward in block.edges:
            coords = shapely.get_coordinates(self.edges.geometry.iloc[edge_id], include_z=True)
            coords = coords if forward else coords[::-1]
            parts.append(coords if not parts else coords[1:])
        return shapely.LineString(np.vstack(parts))

    def _block_name(self, block: Block, line: shapely.LineString) -> str:
        if block.name:
            return block.name
        names = self.edges["name"].iloc[[e for e, _ in block.edges]].dropna()
        if len(names):
            return names.mode().iloc[0]
        return f"path near {self.named[self.named_tree.nearest(line.centroid)]}"

    def _select(self, scored: list[tuple[Suggestion, Block]], top: int):
        """Every feasible track, plus the best `top` other blocks with variety: reserve slots per
        kind and per lap band. Blocks of the same kind and band must be apart."""
        scored = sorted(scored, key=lambda sb: sb[0].score)
        tracks = [sb for sb in scored if sb[0].kind == "track"]
        scored = [sb for sb in scored if sb[0].kind != "track"]
        centre = {id(b): self.node_xy[b.nodes].mean(axis=0) for _, b in scored}
        picked: list[tuple[Suggestion, Block]] = []

        def fits(s, b):
            if len(picked) >= top or any(b is pb for _, pb in picked):
                return False
            return all(
                (s.kind, s.band) != (ps.kind, ps.band)
                or np.hypot(*(centre[id(b)] - centre[id(pb)])) >= MIN_SEPARATION_M
                for ps, pb in picked
            )

        quotas = [("kind", k, 2) for k in ("loop", "out_and_back")]
        quotas += [("band", band, 1) for band in ("short", "medium", "long")]
        for attr, value, count in quotas:
            have = sum(getattr(ps, attr) == value for ps, _ in picked)
            for s, b in scored:
                if have >= count:
                    break
                if getattr(s, attr) == value and fits(s, b):
                    picked.append((s, b))
                    have += 1
        for s, b in scored:
            if fits(s, b):
                picked.append((s, b))
        return sorted(picked + tracks, key=lambda sb: sb[0].score)

    # --- entry point -----------------------------------------------------------------------------

    def plan(self, req: Request) -> list[Suggestion]:
        relaxed = self._penalties(req.mode, TRANSIT)
        strict = self._penalties(req.mode, MAIN)
        graph, pair_edge = self._graph(relaxed, req.easy_pace_s_per_km)
        self.edge_by_pair = pair_edge
        reach_m = REACH_FACTOR * max(req.warmup_m, req.cooldown_m) + req.main_m * MAX_ON_THE_WAY
        limit_s = reach_m * req.easy_pace_s_per_km / 1000 * 1.5

        def transit_from(node: int) -> Transit:
            budgets = (req.warmup_m, req.cooldown_m)
            return self._transit(graph, pair_edge, node, limit_s, relaxed, strict, budgets)

        start = self.snap(req.start)
        wu = transit_from(start)
        cd = wu if req.end is None else transit_from(self.snap(req.end))

        all_nodes = np.arange(len(self.nodes))
        transit_s = wu.leg_s(all_nodes, req.warmup_m) + cd.leg_s(all_nodes, req.cooldown_m)
        blocks = (
            self._stroke_blocks(req, transit_s)
            + self._cycle_blocks(req, start, limit_s)
            + self._track_blocks(req)
        )
        easy_edge = edge_penalty_s(self.edges, req.mode, TRANSIT).to_numpy()
        for block in blocks:
            block.easy_s = self._easy_lap_s(block, easy_edge)
        scored = [(s, b) for b in blocks if (s := self._evaluate(b, req, wu, cd)) is not None]

        picked = []
        for suggestion, block in self._select(scored, req.top):
            suggestion.main_line = self._block_line(block)
            suggestion.name = self._block_name(block, suggestion.main_line)
            suggestion.warmup_nodes = wu.path_nodes(suggestion.access_node)
            suggestion.cooldown_nodes = cd.path_nodes(suggestion.access_node)[::-1]
            picked.append(suggestion)
        return picked
