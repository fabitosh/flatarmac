import pytest

from flatarmac.pace import Runner, Zone, flat_equivalent_m, format_pace, parse_duration


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


def test_flat_equivalent_climbs_cost_more_than_descents_give_back():
    flat = flat_equivalent_m(1000.0, 0.0, 0.0)
    up = flat_equivalent_m(1000.0, 50.0, 0.0)
    down = flat_equivalent_m(1000.0, 0.0, 50.0)
    assert flat == 1000.0
    assert up - flat > flat - down > 0
    assert flat_equivalent_m(100.0, 0.0, 100.0) == 50.0  # floor on steep descents
