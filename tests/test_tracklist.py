from flatarmac.tracklist import is_trail_loop


def test_trail_loops_by_surface_or_name():
    assert is_trail_loop("Track near Hardweg", "woodchips")
    assert is_trail_loop("Finnenbahn Waid", "")
    assert is_trail_loop("Vitaparcour", "")


def test_tartan_track_is_not_a_trail_loop():
    assert not is_trail_loop("Sportanlage Fronwald", "tartan")
    assert not is_trail_loop("Track near Turnerstrasse", "")
