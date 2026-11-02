# flatarmac

Find flat, uninterrupted running routes in Switzerland (swisstopo + OpenStreetMap).
See [PLAN.md](PLAN.md) for the approach and decisions.

## Pipeline

```bash
uv run flatarmac fetch --region zurich   # swissTLM3D roads, swissALTI3D DEM, OSM stops (~0.9 GB download)
uv run flatarmac build --region zurich   # network, gradient/surface/traffic/junction costs
uv run flatarmac export --region zurich  # GeoJSON for the web map
```

Outputs land in `data/<region>/` (git-ignored): `edges.parquet`, `nodes.parquet`, `turns.parquet`.

## Map

```bash
python3 -m http.server 8765
```

Open <http://localhost:8765/web/index.html>.
