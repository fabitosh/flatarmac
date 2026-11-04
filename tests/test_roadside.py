import numpy as np
import pytest
import shapely

from flatarmac.costs import Control, RoadsidePenalty, Sidewalk, TimeOfDay
from flatarmac.roadside import match_lines

# A 100 m road along x; a traffic link drawn 6 m off it, and a road crossing it.
EDGE = shapely.LineString([(0, 0), (100, 0)])
PARALLEL = shapely.LineString([(-20, 6), (120, 6)])
CROSSING = shapely.LineString([(50, -60), (50, 60)])


def test_parallel_line_is_matched_crossing_line_is_not():
    edges = np.array([EDGE])
    assert match_lines(edges, np.array([CROSSING, PARALLEL])).tolist() == [1]
    assert match_lines(edges, np.array([CROSSING])).tolist() == [-1]


def test_line_too_far_away_is_not_matched():
    far = shapely.LineString([(0, 40), (100, 40)])
    assert match_lines(np.array([EDGE]), np.array([far])).tolist() == [-1]


def test_no_sidewalk_on_a_busy_road_costs_a_sidewalk_does_not():
    side = RoadsidePenalty()
    status = [Sidewalk.YES, Sidewalk.NO, Sidewalk.NO, Sidewalk.NOT_A_ROAD]
    vph = [900.0, 900.0, 100.0, np.nan]
    cost = side.space_s_per_km(status, vph, [15.0, 15.0, 15.0, 15.0])
    assert cost[0] == 0.0
    assert cost[1] == pytest.approx(side.no_sidewalk_s_per_km)
    assert 0.0 < cost[2] < cost[1]
    assert cost[3] == 0.0


def test_crossing_costs_more_with_more_traffic():
    side = RoadsidePenalty()
    quiet, busy = (
        side.crossing_s(Control.UNMARKED, vph, 8.0, TimeOfDay.BUSY) for vph in (50.0, 1000.0)
    )
    assert quiet < busy <= side.crossing_range_s[Control.UNMARKED][1]
    # Without a volume the width-class cost applies, halved when quiet.
    assert side.crossing_s(Control.SIGNAL, np.nan, 18.0, TimeOfDay.BUSY) == 18.0
    assert side.crossing_s(Control.SIGNAL, np.nan, 18.0, TimeOfDay.QUIET) == 9.0
