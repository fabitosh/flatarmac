from flatarmac.tracklist import is_trail_loop, round_lap, track_name


def test_trail_loops_by_surface_or_name():
    assert is_trail_loop("Track near Hardweg", "woodchips")
    assert is_trail_loop("Finnenbahn Waid", "")
    assert is_trail_loop("Vitaparcour", "")


def test_tartan_track_is_not_a_trail_loop():
    assert not is_trail_loop("Sportanlage Fronwald", "tartan")
    assert not is_trail_loop("Track near Turnerstrasse", "")


def test_mapped_ring_snaps_to_round_lap():
    assert round_lap(497) == 500
    assert round_lap(245) == 250
    assert round_lap(306) == 300


def test_odd_ring_rounds_to_ten():
    assert round_lap(329) == 330
    assert round_lap(375) in (370, 380)


def test_track_named_after_its_site():
    assert track_name("", "Schule Buhn", "Zürich", "Höhenring", "Zürich") == "Schule Buhn"
    assert (
        track_name("", "Sportanlage Au", "Opfikon", "Austrasse", "Zürich")
        == "Sportanlage Au, Opfikon"
    )


def test_generic_osm_name_gets_its_place():
    assert (
        track_name("100m Bahn", "Schule Lachenzelg", "Zürich", "x", "Zürich")
        == "100m Bahn Schule Lachenzelg"
    )
    assert track_name("Finnenbahn", "", "Erlenbach", "x", "Zürich") == "Finnenbahn Erlenbach"


def test_specific_osm_name_is_kept():
    assert track_name("Finnenbahn Waid", "Schule X", "Zürich", "x", "Zürich") == "Finnenbahn Waid"


def test_street_fallback_without_site():
    assert track_name("", "", "Kloten", "Hardweg", "Zürich") == "Track near Hardweg, Kloten"
