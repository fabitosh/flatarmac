"""Strokes: maximal chains of edges that continue roughly straight through junctions.

A stroke is what a runner would call "running along this road / path". Each edge belongs to exactly
one stroke. At every node, incident edge ends are paired greedily by smallest deflection; unpaired
ends terminate a stroke. Strokes whose ends meet are closed loops.
"""

import pandas as pd

MAX_DEFLECTION_DEG = 60.0


def pair_ends(turns: pd.DataFrame, max_deflection: float = MAX_DEFLECTION_DEG):
    """Map (edge_id, node_id) -> (edge_id, node_id) of the continuing edge end."""
    moves = turns[
        (turns["in_edge"] < turns["out_edge"]) & (turns["deflection_deg"] <= max_deflection)
    ]
    moves = moves.sort_values(["deflection_deg", "turn_s"], kind="stable")
    link: dict[tuple[int, int], tuple[int, int]] = {}
    for node, a, b in zip(moves["node_id"], moves["in_edge"], moves["out_edge"], strict=True):
        end_a, end_b = (a, node), (b, node)
        if end_a in link or end_b in link:
            continue
        link[end_a] = end_b
        link[end_b] = end_a
    return link


def build(edges: pd.DataFrame, turns: pd.DataFrame) -> pd.DataFrame:
    """One row per (stroke, position): stroke_id, seq, edge_id, forward, from_node, to_node, closed."""
    link = pair_ends(turns)
    ends = {e: (u, v) for e, u, v in zip(edges["edge_id"], edges["u"], edges["v"], strict=True)}
    visited: set[int] = set()
    rows: list[tuple] = []

    def walk(edge: int, start_node: int, stroke_id: int) -> None:
        seq, node = 0, start_node
        closed = False
        while True:
            u, v = ends[edge]
            forward = node == u
            other = v if forward else u
            visited.add(edge)
            rows.append((stroke_id, seq, edge, forward, node, other))
            seq += 1
            nxt = link.get((edge, other))
            if nxt is None:
                break
            if nxt[0] in visited:
                closed = nxt == (rows[-seq][2], start_node)
                break
            edge, node = nxt
        if closed:
            rows[-seq:] = [(*r, True) for r in rows[-seq:]]
        else:
            rows[-seq:] = [(*r, False) for r in rows[-seq:]]

    stroke_id = 0
    # Open strokes first: start from an edge end without a continuation.
    for edge, (u, v) in ends.items():
        if edge in visited or u == v:
            continue
        for start in (u, v):
            if (edge, start) not in link:
                walk(edge, start, stroke_id)
                stroke_id += 1
                break
    # Whatever is left forms cycles (or self-loops).
    for edge, (u, _) in ends.items():
        if edge not in visited:
            walk(edge, u, stroke_id)
            stroke_id += 1

    return pd.DataFrame(
        rows, columns=["stroke_id", "seq", "edge_id", "forward", "from_node", "to_node", "closed"]
    )
