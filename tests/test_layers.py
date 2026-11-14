import json

import geopandas as gpd
import shapely

from flatarmac.config import LV95
from flatarmac.layers import to_geojson


def test_geojson_is_compact_2d_wgs84_with_nulls():
    layer = gpd.GeoDataFrame(
        {"edge_id": [0, 1], "name": ["Seestrasse", None], "score": [91, 40]},
        geometry=[
            shapely.LineString([(2_683_000, 1_247_000, 410), (2_683_100, 1_247_000, 411)]),
            shapely.LineString([(2_683_100, 1_247_000, 411), (2_683_100, 1_247_050, 412)]),
        ],
        crs=LV95,
    )
    raw = to_geojson(layer)
    assert b" " not in raw
    features = json.loads(raw)["features"]
    assert [f["properties"] for f in features] == [
        {"edge_id": 0, "name": "Seestrasse", "score": 91},
        {"edge_id": 1, "name": None, "score": 40},
    ]
    lon, lat = features[0]["geometry"]["coordinates"][0]
    assert 8.5 < lon < 8.6 and 47.3 < lat < 47.4
    assert round(lon, 6) == lon


def test_empty_layer_is_an_empty_collection():
    empty = gpd.GeoDataFrame({"name": []}, geometry=[], crs=LV95)
    assert json.loads(to_geojson(empty)) == {"type": "FeatureCollection", "features": []}
