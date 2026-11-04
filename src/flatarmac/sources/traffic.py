"""ARE road traffic loads: vehicles per link from the national passenger transport model.

Per link (both directions together): average daily traffic (DTV) and the morning (7-8h, MSP) and
evening (17-18h, ASP) peak hours, all vehicle types. Covers main and collector roads only.
"""

import geopandas as gpd
import pyogrio

from flatarmac.config import Region

URL = (
    "https://data.geo.admin.ch/ch.are.belastung-personenverkehr-strasse/"
    "belastung-personenverkehr-strasse/belastung-personenverkehr-strasse_2056.gpkg"
)
COLUMNS = {"DTV_FZG": "daily", "MSP_FZG": "morning_peak", "ASP_FZG": "evening_peak"}


def fetch(region: Region) -> gpd.GeoDataFrame:
    # Ranged reads of the national file; without this GDAL also probes for side-car files.
    pyogrio.set_gdal_config_options({"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR"})
    links = pyogrio.read_dataframe(
        "/vsicurl/" + URL, bbox=region.bbox_lv95, columns=list(COLUMNS)
    ).rename(columns=COLUMNS)
    links = links[links.geometry.notna()].reset_index(drop=True)
    region.dir.mkdir(parents=True, exist_ok=True)
    links.to_parquet(region.dir / "traffic_links.parquet")
    return links
