"""Routable network from swissTLM3D road lines.

TLM segments share exact endpoint coordinates at junctions, so nodes are the unique endpoints.
"""

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

# Not legal or not sensible to run on.
EXCLUDED_OBJEKTART = {
    "Autobahn",
    "Autostrasse",
    "Ausfahrt",
    "Einfahrt",
    "Raststaette",
    "Faehre",
    "Autozug",
}
EXCLUDED_RESTRICTION = {"Gesperrt", "Teststrecke"}

EDGE_COLUMNS = {
    "uuid": "tlm_uuid",
    "objektart": "objektart",
    "belagsart": "belagsart",
    "kunstbaute": "kunstbaute",
    "verkehrsbd": "verkehrsbd",
    "kreisel": "kreisel",
    "stufe": "stufe",
    "strname": "name",
}


def build(roads: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return (edges, nodes). Edges carry u/v node ids and the original 3D geometry."""
    keep = ~roads["objektart"].isin(EXCLUDED_OBJEKTART) & ~roads["verkehrsbe"].isin(
        EXCLUDED_RESTRICTION
    )
    edges = roads.loc[keep, [*EDGE_COLUMNS, "geometry"]].rename(columns=EDGE_COLUMNS)
    edges = edges.reset_index(drop=True)

    geoms = edges.geometry.values
    starts = shapely.get_coordinates(shapely.get_point(geoms, 0), include_z=True)
    ends = shapely.get_coordinates(shapely.get_point(geoms, -1), include_z=True)
    endpoints = np.vstack([starts, ends])
    keys = np.round(endpoints[:, :2], 2)
    unique_keys, first, node_ids = np.unique(keys, axis=0, return_index=True, return_inverse=True)

    n = len(edges)
    edges.insert(0, "edge_id", np.arange(n))
    edges.insert(1, "u", node_ids[:n])
    edges.insert(2, "v", node_ids[n:])
    edges["length_m"] = shapely.length(geoms)

    degree = np.bincount(node_ids, minlength=len(unique_keys))
    nodes = gpd.GeoDataFrame(
        pd.DataFrame(
            {
                "node_id": np.arange(len(unique_keys)),
                "z": endpoints[first, 2],
                "degree": degree,
            }
        ),
        geometry=shapely.points(unique_keys),
        crs=edges.crs,
    )
    return edges, nodes
