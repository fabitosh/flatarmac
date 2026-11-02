"""swissALTI3D terrain model, 2 m resolution, mosaicked per region."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import rasterio
from rasterio.merge import merge

from flatarmac.config import RAW_DIR, Region

STAC_ITEMS = "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swissalti3d/items"
RESOLUTION = "2"


def _tile_urls(region: Region) -> dict[str, str]:
    """Latest 2 m GeoTIFF per 1 km tile inside the region, keyed by "EEEE-NNNN" (km)."""
    latest: dict[str, tuple[int, str]] = {}
    url, params = STAC_ITEMS, {"bbox": ",".join(map(str, region.bbox_wgs84)), "limit": 100}
    with httpx.Client(timeout=60) as client:
        while url:
            page = client.get(url, params=params).json()
            for item in page["features"]:
                _, year, tile = item["id"].split("_")
                east_km, north_km = map(int, tile.split("-"))
                inside = (
                    region.east_min <= east_km * 1000 < region.east_max
                    and region.north_min <= north_km * 1000 < region.north_max
                )
                if not inside:
                    continue
                href = next(
                    a["href"]
                    for k, a in item["assets"].items()
                    if k.endswith(f"_{RESOLUTION}_2056_5728.tif")
                )
                if tile not in latest or int(year) > latest[tile][0]:
                    latest[tile] = (int(year), href)
            url = next((link["href"] for link in page["links"] if link["rel"] == "next"), None)
            params = None
    return {tile: href for tile, (_, href) in latest.items()}


def _download(client: httpx.Client, url: str, target: Path) -> Path:
    if not target.exists():
        response = client.get(url)
        response.raise_for_status()
        partial = target.with_suffix(".part")
        partial.write_bytes(response.content)
        partial.rename(target)
    return target


def fetch(region: Region) -> Path:
    tiles = _tile_urls(region)
    expected = (region.east_max - region.east_min) * (region.north_max - region.north_min) // 10**6
    if len(tiles) != expected:
        raise RuntimeError(f"expected {expected} tiles, STAC returned {len(tiles)}")

    tile_dir = RAW_DIR / "swissalti3d"
    tile_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120) as client, ThreadPoolExecutor(8) as pool:
        paths = list(
            pool.map(
                lambda url: _download(client, url, tile_dir / url.rsplit("/", 1)[1]), tiles.values()
            )
        )

    sources = [rasterio.open(p) for p in paths]
    try:
        mosaic, transform = merge(sources, bounds=region.bbox_lv95)
        profile = sources[0].profile | {
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": transform,
            "compress": "deflate",
            "predictor": 3,
            "tiled": True,
        }
    finally:
        for source in sources:
            source.close()

    region.dir.mkdir(parents=True, exist_ok=True)
    out = region.dir / "dem_2m.tif"
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(mosaic)
    return out
