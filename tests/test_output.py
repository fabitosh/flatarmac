import pytest
import shapely

from flatarmac.output import _legs

LINE = shapely.LineString([(0, 0), (100, 0)])


def test_out_and_back_bounces_between_ends():
    legs = list(_legs(LINE, closed=False, start=30.0, total=350.0))
    assert legs == [(30.0, 100.0), (100.0, 0.0), (0.0, 100.0), (100.0, 20.0)]
    assert sum(abs(b - a) for a, b in legs) == pytest.approx(350.0)


def test_loop_wraps_around():
    ring = shapely.LineString([(0, 0), (50, 0), (50, 50), (0, 50), (0, 0)])  # 200 m
    legs = list(_legs(ring, closed=True, start=150.0, total=300.0))
    assert legs == [(150.0, 200.0), (0.0, 200.0), (0.0, 50.0)]
