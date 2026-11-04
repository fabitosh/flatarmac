import pandas as pd

from flatarmac import strokes


def _turns(rows):
    """rows: (node, a, b, deflection) -> symmetric turn table."""
    out = []
    for node, a, b, deflection in rows:
        out.append((node, a, b, deflection, 0.0))
        out.append((node, b, a, deflection, 0.0))
    return pd.DataFrame(
        out, columns=["node_id", "in_edge", "out_edge", "deflection_deg", "turn_s_busy"]
    )


def test_straight_chain_through_t_junction():
    #   0 --e0-- 1 --e1-- 2
    #            |
    #            e2
    #            3
    edges = pd.DataFrame({"edge_id": [0, 1, 2], "u": [0, 1, 1], "v": [1, 2, 3]})
    turns = _turns([(1, 0, 1, 5.0), (1, 0, 2, 90.0), (1, 1, 2, 90.0)])
    result = strokes.build(edges, turns)

    by_stroke = result.groupby("stroke_id")["edge_id"].apply(list).tolist()
    assert sorted(map(sorted, by_stroke)) == [[0, 1], [2]]
    assert not result["closed"].any()


def test_closed_loop():
    # Triangle 0-1-2 with gentle turns everywhere.
    edges = pd.DataFrame({"edge_id": [0, 1, 2], "u": [0, 1, 2], "v": [1, 2, 0]})
    turns = _turns([(1, 0, 1, 10.0), (2, 1, 2, 10.0), (0, 2, 0, 10.0)])
    result = strokes.build(edges, turns)

    assert result["stroke_id"].nunique() == 1
    assert result["closed"].all()
    assert result["from_node"].iloc[0] == result["to_node"].iloc[-1]


def test_orientation_follows_walk():
    # e1 is stored reversed (2 -> 1) but continues e0.
    edges = pd.DataFrame({"edge_id": [0, 1], "u": [0, 2], "v": [1, 1]})
    turns = _turns([(1, 0, 1, 0.0)])
    result = strokes.build(edges, turns).sort_values("seq")

    assert result["from_node"].tolist() in ([0, 1], [2, 1])
    assert result["to_node"].iloc[0] == result["from_node"].iloc[1]
