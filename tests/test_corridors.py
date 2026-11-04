import numpy as np
import pandas as pd
import pytest

from flatarmac.corridors import TurnGraph

# Node 0 is the seed. A busy 1 km road goes straight to node 4; a quiet 1 km path bends via 1, 2, 3.
EDGES = pd.DataFrame(
    {
        "edge_id": [0, 1, 2, 3],
        "u": [0, 0, 1, 2],
        "v": [4, 1, 2, 3],
        "length_m": [1000.0, 400.0, 300.0, 300.0],
    }
)
TURNS = pd.DataFrame(
    [(1, 1, 2, 1.0), (1, 2, 1, 1.0), (2, 2, 3, 1.0), (2, 3, 2, 1.0)],
    columns=["node_id", "in_edge", "out_edge", "turn_s_busy"],
).assign(turn_s_quiet=lambda t: t["turn_s_busy"])


def test_corridor_takes_the_quiet_bent_path():
    graph = TurnGraph(EDGES, TURNS, n_nodes=5)
    penalty = np.array([100.0, 0.0, 0.0, 0.0])
    (corridor,) = graph.corridors(penalty, 300.0, np.array([0]), [1000.0])
    assert corridor.edges == [(1, True), (2, True), (3, True)]
    assert corridor.nodes.tolist() == [0, 1, 2, 3]
    assert corridor.length_m == pytest.approx(1000.0)
    assert corridor.penalty_s == pytest.approx(2.0)  # two turns, no edge penalty
    assert corridor.turn_s == pytest.approx(2.0)
