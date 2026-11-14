import pytest

from flatarmac.config import MAX_RADIUS_KM, Region
from flatarmac.places import slug


def test_region_around_a_point_snaps_outward_to_whole_km():
    region = Region.around("bern", 2_598_633.75, 1_200_386.75, radius_km=8)
    assert region.bbox_lv95 == (2_590_000, 1_192_000, 2_607_000, 1_209_000)


def test_region_radius_is_bounded():
    with pytest.raises(ValueError):
        Region.around("x", 2_600_000, 1_200_000, radius_km=MAX_RADIUS_KM + 1)
    with pytest.raises(ValueError):
        Region.around("x", 2_600_000, 1_200_000, radius_km=0)


def test_place_names_become_region_names():
    assert slug("Zürich Oerlikon") == "zurich-oerlikon"
    assert slug("Ort Road Atlanta - Ruswil") == "ort-road-atlanta-ruswil"
    assert slug("Genève") == "geneve"
