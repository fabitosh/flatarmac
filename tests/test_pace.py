import pytest

from flatarmac.pace import Runner, Zone, format_pace, parse_duration


def test_parse_duration():
    assert parse_duration("18:13") == 1093
    assert parse_duration("1:02:03") == 3723


def test_vdot_from_5k():
    assert Runner.from_5k("18:13").vdot == pytest.approx(55.5, abs=0.1)


def test_zone_paces_are_ordered():
    runner = Runner.from_5k("18:13")
    paces = [
        runner.pace_s_per_km(z) for z in (Zone.EASY, Zone.MARATHON, Zone.THRESHOLD, Zone.INTERVAL)
    ]
    assert paces == sorted(paces, reverse=True)


def test_threshold_pace():
    assert format_pace(Runner.from_5k("18:13").pace_s_per_km(Zone.THRESHOLD)) == "3:54/km"


def test_warmup_distance():
    # 15 min at ~4:55/km easy is roughly 3 km
    assert Runner.from_5k("18:13").distance_m(15 * 60, Zone.EASY) == pytest.approx(3050, abs=60)
