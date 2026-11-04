"""BAFU sonBASE road traffic noise (day and night), 10 m grid, cut to the region.

The national rasters are cloud-optimised GeoTIFFs: only the region's window is read.
"""

from pathlib import Path

import rasterio
from rasterio.windows import from_bounds

from flatarmac.config import Region

BASE = "https://data.geo.admin.ch/ch.bafu.laerm-strassenlaerm_{period}"
URL = BASE + "/laerm-strassenlaerm_{period}/laerm-strassenlaerm_{period}_2056.tif"
PERIODS = ("tag", "nacht")
FILES = {"tag": "noise_day.tif", "nacht": "noise_night.tif"}


def fetch(region: Region) -> list[Path]:
    region.dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
        for period in PERIODS:
            with rasterio.open("/vsicurl/" + URL.format(period=period)) as src:
                window = from_bounds(*region.bbox_lv95, transform=src.transform)
                data = src.read(1, window=window)
                profile = src.profile | {
                    "height": data.shape[0],
                    "width": data.shape[1],
                    "transform": src.window_transform(window),
                    "compress": "deflate",
                    "tiled": True,
                    "blockxsize": 256,
                    "blockysize": 256,
                }
            out = region.dir / FILES[period]
            with rasterio.open(out, "w", **profile) as dst:
                dst.write(data, 1)
            paths.append(out)
    return paths
