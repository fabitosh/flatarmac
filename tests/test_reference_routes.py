"""Reference routes: shapes of runs that should be suggested, from real sessions.

They check a route's shape (a long out-and-back along a river or lake), not exact streets or
scores. Waypoints are river- / lake-level points on the paths. A case passes if the run is in the
top TOP at either time of day: crowds and traffic differ, so the best run differs. Needs a built
region.
"""

import pytest
import shapely
from pyproj import Transformer

from flatarmac.config import LV95, WGS84, ZURICH

pytestmark = [
    pytest.mark.reference,
    pytest.mark.skipif(
        not (ZURICH.dir / "edges.parquet").exists(), reason="needs `flatarmac build`"
    ),
]

TO_LV95 = Transformer.from_crs(WGS84, LV95, always_xy=True)
NEAR_M = 100.0
TOP = 5

CASES = {
    # Home down to the Limmat, downstream past Werdinsel and back. Partly gravel: firm natural.
    "limmat": {
        "start": (47.39893, 8.50662),
        "mode": "firm_natural",
        "main_km": 10,
        "min_one_way_m": 4000,
        "waypoints": [(47.39547, 8.50449), (47.40280, 8.48288)],
    },
    # Bucheggplatz to Bellevue, down the east shore of the lake and back.
    "lake": {
        "start": (47.3979, 8.5300),
        "mode": "tarmac",
        "main_km": 16,
        "min_one_way_m": 5000,
        "waypoints": [(47.35234, 8.55627), (47.33893, 8.56781)],
    },
}


@pytest.fixture(scope="module")
def planner():
    from flatarmac.planner import Planner

    return Planner(ZURICH)


def _point(lat_lon):
    return shapely.Point(TO_LV95.transform(lat_lon[1], lat_lon[0]))


@pytest.mark.parametrize("name", CASES)
def test_long_out_and_back_is_suggested(planner, name):
    from flatarmac.planner import make_request

    case = CASES[name]
    points = [_point(p) for p in case["waypoints"]]

    def matches(s):
        return (
            s.kind == "out_and_back"
            and s.lap_m / 2 >= case["min_one_way_m"]
            and all(s.main_line.distance(p) <= NEAR_M for p in points)
        )

    ranks, tops = {}, {}
    for time in ("quiet", "busy"):
        req = make_request(
            case["start"], case["main_km"], "km", 15, 10, "18:13", mode=case["mode"], time=time
        )
        suggestions = planner.plan(req)
        ranks[time] = [rank for rank, s in enumerate(suggestions, start=1) if matches(s)]
        tops[time] = [f"{s.kind} {s.name} lap {s.lap_m:.0f} m" for s in suggestions[:TOP]]
    best = min((r[0] for r in ranks.values() if r), default=None)
    assert best is not None and best <= TOP, f"{name}: match at ranks {ranks}; top {TOP}: {tops}"
