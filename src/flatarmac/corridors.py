"""Corridors: long out-and-backs along the most runnable path of a given length.

Unlike strokes, a corridor may follow any sequence of roads and paths, across junctions and name
changes (a river path that switches banks, a lake promenade that changes names). It is searched on
a graph whose nodes are directed edges ("pieces") and whose links are the movements of the turn
table, so corner and crossing costs are exact. Each search starts at a seed near where the warm-up
ends and keeps, per target length, the path with the lowest penalty per km, wherever it ends.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

# While searching, each piece also costs this share of its easy-pace time, so 1 penalty second is
# worth about 34 m of extra path (the warm-up router uses ~3.4 m). Low enough to follow a lake
# promenade instead of the direct street, high enough to keep corridors from wandering.
LENGTH_WEIGHT = 0.1
SEED_CELL_M = 1000.0  # one seed per grid cell around where the warm-up ends
MAX_SEEDS = 60
SEED_RING = (0.3, 1.2)  # seeds where this share of the warm-up budget is used up
LENGTH_TOLERANCE = 0.1  # corridor length within ±10 % of the target
PENALTY_BUDGET_S_PER_KM = 30.0  # search stops once paths are this bad on average
SEARCH_REACH = 1.5  # ... or this many times the longest target out
MAX_SHARED = 0.5  # corridors sharing more of their edges than this are duplicates
MIN_ONE_WAY_M = 1000.0


@dataclass
class Corridor:
    edges: list[tuple[int, bool]]  # (edge_id, forward), one way
    nodes: np.ndarray
    length_m: float
    penalty_s: float  # one way, incl. turns
    turn_s: float  # part of penalty_s from turns and crossings


def _path_sums(values: np.ndarray, pred: np.ndarray, reached: np.ndarray) -> np.ndarray:
    """Sum of `values` along each reached node's tree path, by pointer doubling."""
    total = np.where(reached, values, 0.0)
    up = np.where(reached & (pred >= 0), pred, -1)
    while True:
        idx = np.flatnonzero(up >= 0)
        if not len(idx):
            return total
        parents = up[idx]
        add, nxt = total[parents], up[parents]
        total[idx] += add
        up[idx] = nxt


def seeds(
    node_xy: np.ndarray, flat_m: np.ndarray, warmup_m: float, node_s_per_km: np.ndarray
) -> np.ndarray:
    """Nodes to start corridors from: one per grid cell around where the warm-up ends, the one
    with the most runnable road (then the closest to the end of the warm-up)."""
    lo, hi = SEED_RING[0] * warmup_m, max(SEED_RING[1] * warmup_m, 2 * SEED_CELL_M)
    nodes = np.flatnonzero((flat_m >= lo) & (flat_m <= hi))
    cells = np.floor(node_xy[nodes] / SEED_CELL_M).astype(int)
    frame = pd.DataFrame(
        {
            "node": nodes,
            "cx": cells[:, 0],
            "cy": cells[:, 1],
            "quality": node_s_per_km[nodes],
            "off": np.abs(flat_m[nodes] - warmup_m),
        }
    )
    frame = frame.sort_values(["quality", "off"]).drop_duplicates(["cx", "cy"])
    return frame["node"].to_numpy()[:MAX_SEEDS]


class TurnGraph:
    """Pieces (2 * edge_id + backward) linked by the movements of the turn table, no U-turns."""

    def __init__(self, edges: pd.DataFrame, turns: pd.DataFrame, n_nodes: int):
        u, v = edges["u"].to_numpy(), edges["v"].to_numpy()
        self.n = 2 * len(edges)
        self.length = np.repeat(edges["length_m"].to_numpy(), 2)
        self.start_node = np.empty(self.n, dtype=np.int64)
        self.end_node = np.empty(self.n, dtype=np.int64)
        self.start_node[0::2], self.start_node[1::2] = u, v
        self.end_node[0::2], self.end_node[1::2] = v, u

        moves = turns[turns["in_edge"] != turns["out_edge"]]
        node = moves["node_id"].to_numpy()
        a, b = moves["in_edge"].to_numpy(), moves["out_edge"].to_numpy()
        self.src = 2 * a + (v[a] != node)  # arriving along a: forward if a ends here
        self.dst = 2 * b + (u[b] != node)  # leaving along b: forward if b starts here
        self.turn = moves["turn_s"].to_numpy()

        order = np.argsort(self.start_node, kind="stable")
        bounds = np.searchsorted(self.start_node[order], np.arange(n_nodes + 1))
        self._leaving = order
        self._bounds = bounds

    def leaving(self, node: int) -> np.ndarray:
        return self._leaving[self._bounds[node] : self._bounds[node + 1]]

    def corridors(
        self,
        edge_penalty: np.ndarray,
        easy_pace_s_per_km: float,
        seed_nodes: np.ndarray,
        targets_m: list[float],
    ) -> list[Corridor]:
        """Per seed and target length, the path with the lowest penalty per km. Per target,
        duplicates (corridors mostly on the same edges) are removed; best first."""
        if not targets_m:
            return []
        piece_pen = np.repeat(edge_penalty, 2)
        step_per_m = LENGTH_WEIGHT * easy_pace_s_per_km / 1000
        weight = piece_pen[self.dst] + step_per_m * self.length[self.dst] + self.turn
        # csgraph treats explicit zeros as missing links.
        graph = csr_matrix((np.maximum(weight, 1e-6), (self.src, self.dst)), shape=(self.n, self.n))
        reach_m = SEARCH_REACH * max(targets_m)
        limit = (PENALTY_BUDGET_S_PER_KM / 1000 + step_per_m) * reach_m

        found: dict[float, list[Corridor]] = {target: [] for target in targets_m}
        for seed in seed_nodes:
            sources = self.leaving(int(seed))
            if not len(sources):
                continue
            dist, pred, source = dijkstra(
                graph, indices=sources, min_only=True, limit=limit, return_predecessors=True
            )
            reached = np.isfinite(dist)
            idx = np.flatnonzero(reached)
            cum_len = _path_sums(self.length, pred, reached)
            cum_piece_pen = _path_sums(piece_pen, pred, reached)
            # `dist` covers every piece after the source (penalty + length weight) and every turn.
            penalty = np.full(self.n, np.inf)
            src = source[idx]
            penalty[idx] = (
                dist[idx] + piece_pen[src] - step_per_m * (cum_len[idx] - self.length[src])
            )
            per_km = np.divide(penalty, cum_len, out=np.full(self.n, np.inf), where=cum_len > 0)
            for target in targets_m:
                window = reached & (np.abs(cum_len - target) <= LENGTH_TOLERANCE * target)
                if not window.any():
                    continue
                end = int(np.argmin(np.where(window, per_km, np.inf)))
                path = [end]
                while pred[path[-1]] >= 0:
                    path.append(int(pred[path[-1]]))
                path.reverse()
                pieces = np.array(path)
                found[target].append(
                    Corridor(
                        edges=[(int(p // 2), p % 2 == 0) for p in pieces],
                        nodes=np.append(self.start_node[pieces[0]], self.end_node[pieces]),
                        length_m=float(cum_len[end]),
                        penalty_s=float(penalty[end]),
                        turn_s=float(penalty[end] - cum_piece_pen[end]),
                    )
                )
        return [c for per_target in found.values() for c in _distinct(per_target)]


def _distinct(found: list[Corridor]) -> list[Corridor]:
    found = sorted(found, key=lambda c: c.penalty_s / c.length_m)
    kept: list[tuple[Corridor, set[int]]] = []
    for corridor in found:
        edge_set = {e for e, _ in corridor.edges}
        if all(
            len(edge_set & other) <= MAX_SHARED * min(len(edge_set), len(other))
            for _, other in kept
        ):
            kept.append((corridor, edge_set))
    return [c for c, _ in kept]
