# flatarmac

Find flat, uninterrupted running routes in Switzerland (swisstopo + OpenStreetMap).
See [PLAN.md](PLAN.md) for the approach and decisions.

## Install and run

flatarmac runs on your own computer: it downloads open data for the area you run in, builds the
route network there, and opens a map in your browser. Nothing is hosted or shared.

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (macOS / Linux:
   `curl -LsSf https://astral.sh/uv/install.sh | sh`; Windows:
   `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`).
2. Install flatarmac (uv brings its own Python):

   ```bash
   uv tool install git+https://github.com/fabitosh/flatarmac
   ```

3. Get the area you run in, then open the map:

   ```bash
   flatarmac download                  # prebuilt greater Zürich (~90 MB), ready in a minute
   flatarmac setup "Bern"              # or build any other area: place, municipality or postcode
   flatarmac serve                     # opens http://localhost:8765
   ```

   Greater Zürich covers Baden and Bremgarten to Wolfhausen, Horgen to north of Winterthur.
   `setup` builds 8 km around a place by default (`--radius`, at most 15 km).

`setup` takes a while the first time: the swissTLM3D road dataset (~0.9 GB download, ~3 GB on
disk) is shared by all regions, and OpenStreetMap queries go to public Overpass servers that can be
slow. If it stops, run the same command again: it picks up where it left off. Data lives in
`FLATARMAC_DATA` if set, else your user data folder (`~/.local/share/flatarmac` on Linux,
`~/Library/Application Support/flatarmac` on macOS).

Update with `uv tool upgrade flatarmac`. With several regions set up, pick one with
`flatarmac --region bern serve`.

## Development

From a checkout, `uv run flatarmac ...` keeps data in `data/` and the curated lists in
`src/flatarmac/curated/`. The pipeline steps that `setup` runs can also be run one by one:

```bash
uv run flatarmac --region zurich fetch   # roads, DEM, OSM, road noise, traffic loads
uv run flatarmac --region zurich build   # network, gradient/surface/traffic/junction costs
```

Outputs land in `data/<region>/` (git-ignored): `edges.parquet`, `nodes.parquet`, `turns.parquet`.
These tables are the only copy: `serve` builds the map layers from them when it starts (open
`edges.parquet` in QGIS for every attribute).

`uv run flatarmac --region zurich bundle` packs the tables planning needs into
`data/dist/region-zurich.tar.gz`; attached to a GitHub release, that is what `flatarmac download`
installs.

## Route suggestions

```bash
uv run flatarmac plan --start 47.3779,8.5403 --main 8km --warmup 15 --cooldown 10 --name hb
uv run flatarmac plan --start 47.3913,8.4889 --main 25min --main-zone threshold --name altstetten
uv run flatarmac plan --start 47.3667,8.5650 --mode firm_natural --name hottingen
uv run flatarmac plan --start 47.3979,8.5300 --main 16km --time quiet --name sunday
```

`--time` (and Quiet / Busy in the web form) sets how busy the streets are. Running beside traffic
costs road noise (sonBASE, a mild nudge), no room to run (a busy road without a sidewalk) and
crowds (pedestrian streets, shops and cafés, stations); crossings cost by the crossed road's
traffic. Quiet is early morning, evening or Sunday: night noise, off-peak traffic, few people. Busy
is weekday daytime or Saturday: day noise, the evening peak hour, full streets.

Durations are converted with paces from `--five-k` (default 18:13). Warm-up/cool-down are routed with
relaxed penalties; whatever the way to the block doesn't cover is run easy on the block itself.
Results go to `data/<region>/routes/<name>.geojson` plus one GPX per suggestion; view them with
`flatarmac serve` at <http://localhost:8765/?routes=<name>>.

## Web UI

```bash
uv run flatarmac serve
```

It opens <http://localhost:8765> (`--no-browser` to skip that). Click the map to set a start (optionally a different finish), set the
main block, warm-up, cool-down and 5k time, then **Find routes**. Each suggestion has a GPX download.

## Tracks

Tracks are highlighted on the map, and every track within reach is always suggested on top of
the `--top` other suggestions. They come from the hand-editable list `tracks_<region>.csv`
(`lap_m`, `include`, `name`, `surface`): in `src/flatarmac/curated/` in a checkout, in the data
folder's `curated/` when installed. `setup` fills it from OpenStreetMap for a new region. Run
`flatarmac build` after editing. To pick up new OSM tracks:

```bash
uv run flatarmac tracks --discover          # query Overpass for leisure=track (after fetch)
uv run flatarmac tracks --add relation/123  # add one by OSM id
```

## Closed roads

Roads the map shows as open but that are not (fenced sites, long-closed paths) go into
`closures_<region>.csv` (next to the track lists) by swissTLM3D `tlm_uuid`, with a `note` on why. The build leaves
them out of the network.

## Data and attribution

Nothing downloaded is committed or redistributed; `setup` and `fetch` pull it into the data folder.

- Roads (swissTLM3D), elevation (swissALTI3D) and the base map: © [swisstopo](https://www.swisstopo.admin.ch/),
  open government data.
- Road traffic noise (sonBASE): © [BAFU](https://www.bafu.admin.ch/), and road traffic loads
  (national passenger transport model): © [ARE](https://www.are.admin.ch/), both open government
  data via [geo.admin.ch](https://data.geo.admin.ch/).
- Stops, crossings, sidewalks, pedestrian areas, shops, stations and running tracks:
  © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, ODbL. `tracks_<region>.csv` is derived from OpenStreetMap and shared under
  the same licence.
