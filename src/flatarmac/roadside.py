"""What lies beside each TLM edge: noise, traffic volume, sidewalk and crowds.

swissTLM3D knows a road's width, not its traffic. Other sources are matched onto its edges:
sonBASE noise (sampled along the edge), ARE traffic-model links and OSM roads (matched as parallel
lines), and OSM pedestrian areas, shops and stations (crowd index).
"""

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely

from flatarmac.costs import RoadClass, Sidewalk, TimeOfDay

SAMPLE_M = 10.0
NOISE_SAMPLE_M = 5.0
MATCH_M = 15.0  # a parallel line within this distance runs alongside the edge
MATCH_DEG = 30.0
MIN_SHARE = 0.5  # of the edge's samples
BEARING_STEP_M = 2.0

# Quiet uses off-peak traffic: the daily total spread over 24 h, halved.
QUIET_SHARE_OF_HOURLY_MEAN = 0.5
SIDEWALK_YES = {"both", "left", "right", "yes", "separate"}
SIDEWALK_NO = {"no", "none"}
PEDESTRIAN_HIGHWAYS = {"pedestrian", "living_street"}
# Crowd index: shops and cafés within reach per 100 m of edge for a full crowd; station radius.
POI_REACH_M = 30.0
POIS_PER_100M_FULL = 8.0
STATION_RADIUS_M = 150.0
STATION_CROWD = 0.6
PEDESTRIAN_CROWD = 0.5  # a pedestrian street by itself; shops and cafés make it a full crowd


def _samples(lines: np.ndarray, step: float):
    """Points every `step` metres along each line (at least the midpoint): (owner, points, along)."""
    lengths = shapely.length(lines)
    counts = np.maximum(np.floor(lengths / step).astype(int), 1)
    owner = np.repeat(np.arange(len(lines)), counts)
    first = np.repeat(np.cumsum(counts) - counts, counts)
    k = np.arange(len(owner)) - first
    along = (k + 0.5) * lengths[owner] / counts[owner]
    return owner, shapely.line_interpolate_point(lines[owner], along), along


def _bearing(lines: np.ndarray, along: np.ndarray) -> np.ndarray:
    """Undirected bearing (0-180°) of each line at the given distance along it."""
    length = shapely.length(lines)
    a = shapely.line_interpolate_point(lines, np.clip(along - BEARING_STEP_M, 0, length))
    b = shapely.line_interpolate_point(lines, np.clip(along + BEARING_STEP_M, 0, length))
    d = shapely.get_coordinates(b) - shapely.get_coordinates(a)
    return np.degrees(np.arctan2(d[:, 1], d[:, 0])) % 180


def match_lines(
    edges: np.ndarray,
    lines: np.ndarray,
    max_m: float = MATCH_M,
    max_deg: float = MATCH_DEG,
    min_share: float = MIN_SHARE,
) -> np.ndarray:
    """For each edge, the index of the line running alongside it, else -1.

    Samples every SAMPLE_M along the edge vote for their nearest line if it is within `max_m` and
    roughly parallel there; the line with most votes wins if it covers `min_share` of the samples.
    """
    match = np.full(len(edges), -1)
    if len(lines) == 0 or len(edges) == 0:
        return match
    owner, points, along = _samples(edges, SAMPLE_M)
    pairs = shapely.STRtree(lines).query_nearest(points, max_distance=max_m, all_matches=False)
    sample, line = pairs
    edge_deg = _bearing(edges[owner[sample]], along[sample])
    line_deg = _bearing(lines[line], shapely.line_locate_point(lines[line], points[sample]))
    diff = np.abs(edge_deg - line_deg)
    parallel = np.minimum(diff, 180 - diff) <= max_deg
    votes = pd.DataFrame({"edge": owner[sample][parallel], "line": line[parallel]})
    if votes.empty:
        return match
    counts = votes.groupby(["edge", "line"]).size().rename("n").reset_index()
    best = counts.sort_values("n", ascending=False).drop_duplicates("edge")
    total = np.bincount(owner, minlength=len(edges))
    best = best[best["n"] >= min_share * total[best["edge"]]]
    match[best["edge"].to_numpy()] = best["line"].to_numpy()
    return match


def noise_db(edges: np.ndarray, raster_path) -> np.ndarray:
    """Median noise level along each edge (NaN where sonBASE has no value, e.g. underground)."""
    owner, points, _ = _samples(edges, NOISE_SAMPLE_M)
    xy = shapely.get_coordinates(points)
    with rasterio.open(raster_path) as src:
        grid = src.read(1).astype(float)
        rows, cols = rasterio.transform.rowcol(src.transform, xy[:, 0], xy[:, 1])
        nodata = src.nodata
    rows = np.clip(np.asarray(rows), 0, grid.shape[0] - 1)
    cols = np.clip(np.asarray(cols), 0, grid.shape[1] - 1)
    values = grid[rows, cols]
    values[(values == nodata) | (values <= 0)] = np.nan
    return pd.Series(values).groupby(owner).median().reindex(range(len(edges))).to_numpy()


def _sidewalk(roads: gpd.GeoDataFrame) -> np.ndarray:
    """Sidewalk status of OSM road ways from their tags."""
    tags = roads[["sidewalk", "sidewalk:both", "sidewalk:left", "sidewalk:right"]]
    yes = tags.isin(SIDEWALK_YES).any(axis=1)
    no = roads["sidewalk"].isin(SIDEWALK_NO) | (
        roads["sidewalk:both"].isin(SIDEWALK_NO)
        | (roads["sidewalk:left"].isin(SIDEWALK_NO) & roads["sidewalk:right"].isin(SIDEWALK_NO))
    )
    status = np.where(yes, Sidewalk.YES, np.where(no, Sidewalk.NO, Sidewalk.UNKNOWN))
    return np.where(roads["highway"].isin(PEDESTRIAN_HIGHWAYS), Sidewalk.PEDESTRIAN, status)


def _crowd(edges: gpd.GeoDataFrame, walk: gpd.GeoDataFrame, pedestrian: np.ndarray) -> np.ndarray:
    """0-1: pedestrian street or area, shop and café density, or near a railway station."""
    geoms = edges.geometry.values
    length = np.maximum(edges["length_m"].to_numpy(), 1.0)

    areas = walk[walk["kind"] == "pedestrian_area"].geometry
    in_area = np.zeros(len(edges))
    if len(areas):
        union = shapely.union_all(areas.values)
        in_area = shapely.length(shapely.intersection(geoms, union)) / length

    pois = walk[walk["kind"] == "poi"].geometry.values
    edge_idx, _ = shapely.STRtree(pois).query(geoms, predicate="dwithin", distance=POI_REACH_M)
    per_100m = np.bincount(edge_idx, minlength=len(edges)) / length * 100
    shops = np.clip(per_100m / POIS_PER_100M_FULL, 0.0, 1.0)

    stations = walk[walk["kind"] == "station"].geometry.values
    near, _ = shapely.STRtree(stations).query(geoms, predicate="dwithin", distance=STATION_RADIUS_M)
    station = np.zeros(len(edges))
    station[np.unique(near)] = STATION_CROWD

    street = PEDESTRIAN_CROWD * pedestrian.astype(float)
    return np.maximum.reduce([street, PEDESTRIAN_CROWD * in_area, shops, station])


def attributes(
    edges: gpd.GeoDataFrame,
    walk: gpd.GeoDataFrame,
    links: gpd.GeoDataFrame,
    noise_paths: dict[TimeOfDay, str],
) -> pd.DataFrame:
    """Per edge: noise_db_<time>, vph_<time>, sidewalk, crowd."""
    geoms = edges.geometry.values
    out = pd.DataFrame(index=edges.index)
    for time, path in noise_paths.items():
        out[f"noise_db_{time}"] = noise_db(geoms, path)

    road = (edges["road_class"] != RoadClass.PATH).to_numpy()
    link = np.full(len(edges), -1)
    link[road] = match_lines(geoms[road], links.geometry.values)
    hourly = {
        TimeOfDay.QUIET: links["daily"].to_numpy() / 24 * QUIET_SHARE_OF_HOURLY_MEAN,
        TimeOfDay.BUSY: links["evening_peak"].to_numpy().astype(float),
    }
    for time, vph in hourly.items():
        out[f"vph_{time}"] = np.where(link >= 0, vph[link], np.nan)

    roads = walk[walk["kind"] == "road"].reset_index(drop=True)
    osm = match_lines(geoms, roads.geometry.values)
    pedestrian_street = (osm >= 0) & (roads["highway"].to_numpy()[osm] == "pedestrian")
    status = np.array(_sidewalk(roads), dtype=object)
    sidewalk = np.where(osm >= 0, status[osm], Sidewalk.NOT_A_ROAD)
    sidewalks = walk[walk["kind"] == "sidewalk"].geometry.values
    alongside = match_lines(geoms, sidewalks, min_share=0.3) >= 0
    sidewalk = np.where(alongside & (sidewalk != Sidewalk.PEDESTRIAN), Sidewalk.YES, sidewalk)
    # Traffic without a mapped OSM road (geometry offsets) still is a road.
    sidewalk = np.where((sidewalk == Sidewalk.NOT_A_ROAD) & (link >= 0), Sidewalk.UNKNOWN, sidewalk)
    # Paths are never roads, whatever runs alongside them.
    out["sidewalk"] = np.where(road, sidewalk, Sidewalk.NOT_A_ROAD).astype(str)
    out["crowd"] = _crowd(edges, walk, pedestrian_street)
    return out
