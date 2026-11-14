"""Prebuilt regions: pack a built network for a GitHub release, and install one from there.

A bundle holds the parquet tables planning and the map need, plus `region.json`.
"""

import io
import json
import tarfile
from dataclasses import asdict
from pathlib import Path

import httpx

from flatarmac.config import DATA_DIR, Region

RELEASES = "https://github.com/fabitosh/flatarmac/releases/latest/download"
TABLES = ["edges.parquet", "nodes.parquet", "turns.parquet", "strokes.parquet", "tracks.parquet"]
ATTRIBUTION = """\
Prebuilt flatarmac network. Built from:

- swissTLM3D roads and swissALTI3D elevation: (c) swisstopo, open government data
  (https://www.swisstopo.admin.ch/)
- sonBASE road traffic noise: (c) BAFU; road traffic loads: (c) ARE; open government data via
  https://data.geo.admin.ch/
- Stops, crossings, sidewalks, pedestrian areas, shops, stations and running tracks:
  (c) OpenStreetMap contributors (https://www.openstreetmap.org/copyright)

As a database derived from OpenStreetMap, this bundle is made available under the Open Database
Licence (ODbL) 1.0: https://opendatacommons.org/licenses/odbl/1-0/
"""


def asset_name(region_name: str) -> str:
    return f"region-{region_name}.tar.gz"


def _add_text(tar: tarfile.TarFile, name: str, text: str) -> None:
    data = text.encode()
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def pack(region: Region, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / asset_name(region.name)
    with tarfile.open(path, "w:gz") as tar:
        for table in TABLES:
            tar.add(region.dir / table, arcname=f"{region.name}/{table}")
        region_json = json.dumps(asdict(region), ensure_ascii=False) + "\n"
        _add_text(tar, f"{region.name}/region.json", region_json)
        _add_text(tar, f"{region.name}/ATTRIBUTION.txt", ATTRIBUTION)
    return path


def install(region_name: str) -> Region:
    """Download the region's bundle from the latest release and unpack it into the data dir."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    partial = DATA_DIR / f".{asset_name(region_name)}.part"
    url = f"{RELEASES}/{asset_name(region_name)}"
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
        if response.status_code == 404:
            raise LookupError(f"no prebuilt {region_name!r} in the latest release")
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        with partial.open("wb") as out:
            for chunk in response.iter_bytes(1 << 20):
                out.write(chunk)
                if total:
                    print(f"\r{out.tell() / 1e6:.0f} / {total / 1e6:.0f} MB", end="", flush=True)
    print()

    with tarfile.open(partial) as tar:
        outside = [m.name for m in tar.getmembers() if not m.name.startswith(f"{region_name}/")]
        if outside:
            raise ValueError(f"unexpected files in bundle: {outside[:3]}")
        tar.extractall(DATA_DIR, filter="data")
    partial.unlink()
    return Region(**json.loads((DATA_DIR / region_name / "region.json").read_text()))
