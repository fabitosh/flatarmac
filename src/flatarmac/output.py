"""Turn suggestions into full run geometries: GeoJSON for the map, GPX for watches."""

import itertools
import json
from pathlib import Path
from xml.sax.saxutils import escape

import geopandas as gpd
import numpy as np
import shapely
from pyproj import Transformer
from shapely.ops import substring

from flatarmac.config import LV95, WGS84
from flatarmac.planner import Planner, Request, Suggestion

TO_WGS84 = Transformer.from_crs(LV95, WGS84, always_xy=True)


def _path_line(planner: Planner, nodes: list[int]) -> shapely.LineString | None:
    """Oriented 3D line along the cheapest edges between consecutive nodes."""
    if len(nodes) < 2:
        return None
    edges = planner.edges
    by_pair = planner.edge_by_pair
    parts = []
    for a, b in itertools.pairwise(nodes):
        edge = by_pair[(a, b)]
        coords = shapely.get_coordinates(edges.geometry.iloc[edge], include_z=True)
        coords = coords if edges["u"].iloc[edge] == a else coords[::-1]
        parts.append(coords if not parts else coords[1:])
    return shapely.LineString(np.vstack(parts))


def _legs(line: shapely.LineString, closed: bool, start: float, total: float):
    """Positions along `line` for running `total` metres from `start`: back and forth for an
    out-and-back stretch, round and round for a closed ring. Yields (from, to) pairs."""
    length = line.length
    position, remaining = start, total
    direction = 1
    while remaining > 1e-6:
        if closed:
            step = min(length - position, remaining)
            yield position, position + step
            position, remaining = (position + step) % length, remaining - step
        else:
            target = length if direction > 0 else 0.0
            step = min(abs(target - position), remaining)
            end = position + direction * step
            yield position, end
            position, remaining = end, remaining - step
            if position in (0.0, length):
                direction = -direction


def main_block_line(s: Suggestion, planner: Planner, main_m: float) -> shapely.LineString:
    access = planner.nodes.geometry.iloc[s.access_node]
    closed = s.kind != "out_and_back"
    start = s.main_line.project(access)
    legs = list(_legs(s.main_line, closed, start, main_m))
    if not closed and abs(legs[-1][1] - start) > 1:
        # Jog back to the access point along the stretch, where the cool-down starts.
        legs.append((legs[-1][1], start))
    coords = []
    for a, b in legs:
        piece = substring(s.main_line, a, b) if a <= b else substring(s.main_line, b, a).reverse()
        part = shapely.get_coordinates(piece, include_z=True)
        coords.append(part if not coords else part[1:])
    coords = np.vstack(coords)
    if access.distance(s.main_line) > 1:
        # Tracks: connector from the network node onto the track and back.
        connector = np.array([[access.x, access.y, coords[0, 2]]])
        coords = np.vstack([connector, coords, connector])
    return shapely.LineString(coords)


def full_run(s: Suggestion, planner: Planner, main_m: float) -> dict[str, shapely.LineString]:
    legs = {
        "warmup": _path_line(planner, s.warmup_nodes),
        "main": main_block_line(s, planner, main_m),
        "cooldown": _path_line(planner, s.cooldown_nodes),
    }
    return {k: v for k, v in legs.items() if v is not None}


def _to_wgs84(line: shapely.LineString) -> shapely.LineString:
    coords = shapely.get_coordinates(line, include_z=True)
    lon, lat = TO_WGS84.transform(coords[:, 0], coords[:, 1])
    return shapely.LineString(np.column_stack([lon, lat, coords[:, 2]]))


def write(
    suggestions: list[Suggestion], planner: Planner, req: Request, out_dir: Path, name: str
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    main_m = req.main_m
    rows = []
    for rank, s in enumerate(suggestions, start=1):
        # Warm-up / cool-down not covered by the way there and back is run easy on the block.
        easy_on_block_m = max(req.warmup_m - s.warmup_m, 0) + max(req.cooldown_m - s.cooldown_m, 0)
        legs = full_run(s, planner, main_m + easy_on_block_m)
        props = {
            "rank": rank,
            "kind": s.kind,
            "name": s.name,
            "lap_m": round(s.lap_m),
            "laps": round(s.laps, 1),
            "main_s_per_km": round(s.main_s / (main_m / 1000), 1),
            "warmup_m": round(s.warmup_m),
            "cooldown_m": round(s.cooldown_m),
            "easy_on_block_m": round(easy_on_block_m),
            "total_m": round(s.warmup_m + s.cooldown_m + main_m + easy_on_block_m),
            "score": round(s.score, 1),
        }
        for leg, line in legs.items():
            rows.append({**props, "leg": leg, "geometry": line})
        _write_gpx(out_dir / f"{name}_{rank}.gpx", f"{name} #{rank}: {s.name}", legs)

    path = out_dir / f"{name}.geojson"
    if rows:
        frame = gpd.GeoDataFrame(rows, crs=LV95).to_crs(WGS84)
        frame.to_file(path, driver="GeoJSON", COORDINATE_PRECISION=6)
    else:
        path.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    (out_dir / "index.json").write_text(
        json.dumps(sorted(p.stem for p in out_dir.glob("*.geojson")))
    )
    return path


def _write_gpx(path: Path, title: str, legs: dict[str, shapely.LineString]) -> None:
    points = []
    for line in legs.values():
        for lon, lat, ele in shapely.get_coordinates(_to_wgs84(line), include_z=True):
            points.append(f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>{ele:.1f}</ele></trkpt>')
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="flatarmac" xmlns="http://www.topografix.com/GPX/1/1">\n'
        f"<trk><name>{escape(title)}</name><trkseg>\n"
        + "\n".join(points)
        + "\n</trkseg></trk>\n</gpx>\n"
    )
