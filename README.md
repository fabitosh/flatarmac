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

## Route suggestions

```bash
uv run flatarmac plan --start 47.3779,8.5403 --main 8km --warmup 15 --cooldown 10 --name hb
uv run flatarmac plan --start 47.3913,8.4889 --main 25min --main-zone threshold --name altstetten
uv run flatarmac plan --start 47.3667,8.5650 --mode firm_natural --name hottingen
```

Durations are converted with paces from `--five-k` (default 18:13). Warm-up/cool-down are routed with
relaxed penalties; whatever the way to the block doesn't cover is run easy on the block itself.
Results go to `data/<region>/routes/<name>.geojson` plus one GPX per suggestion; view them at
`web/index.html?routes=<name>`.

## Web UI

```bash
uv run flatarmac serve
```

Open <http://localhost:8765>, click the map to set a start (optionally a different finish), set the
main block, warm-up, cool-down and 5k time, then **Find routes**. Each suggestion has a GPX download.

## Tracks

Tracks are highlighted on the map, and every track within reach is always suggested on top of
the `--top` other suggestions. They come from the hand-editable list `curated/tracks_<region>.csv` (`lap_m`, `include`,
`name`, `surface`). Run `uv run flatarmac build` after editing. To pick up new OSM tracks:

```bash
uv run flatarmac tracks --discover          # query Overpass for leisure=track (needs a built region)
uv run flatarmac tracks --add relation/123  # add one by OSM id
```
