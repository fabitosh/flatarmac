"""Place names to LV95 points via the swisstopo search service (no key needed)."""

import re
import unicodedata

import httpx

SEARCH = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
# Municipalities first, then places from the swissNAMES3D gazetteer, then postcodes.
ORIGINS = "gg25,gazetteer,zipcode"


def lookup(text: str) -> tuple[float, float, str]:
    """(east, north, label) of the best match, e.g. "Bern" -> (2598634, 1200387, "Bern")."""
    params = {"searchText": text, "type": "locations", "origins": ORIGINS, "sr": 2056, "limit": 1}
    results = httpx.get(SEARCH, params=params, timeout=30).raise_for_status().json()["results"]
    if not results:
        raise LookupError(f"no place in Switzerland matches {text!r}")
    attrs = results[0]["attrs"]
    # Labels look like "<b>Bern (BE)</b>"; the search swaps x and y relative to LV95 east/north.
    label = re.sub(r"<[^>]+>|\s*\([A-Z]{2}\)", "", attrs["label"]).strip()
    return attrs["y"], attrs["x"], label


def slug(text: str) -> str:
    """Region name for a place: "Zürich Oerlikon" -> "zurich-oerlikon"."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
