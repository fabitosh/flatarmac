# flatarmac — plan

Find flat, uninterrupted running routes in Switzerland. Tarmac mode (default on) prefers hard surfaces.
Gradients and interruptions are tolerated during warm-up / cool-down.

## 1. Problem definition

A run is a sequence of phases, each with its own cost function:

```
start ──(warm-up: relaxed)──▶ main block (strict) ──(cool-down: relaxed)──▶ end
```

| Phase       | Duration source           | Gradient    | Interruptions | Surface (tarmac mode) |
|-------------|---------------------------|-------------|---------------|-----------------------|
| Warm-up     | minutes × easy pace       | tolerated   | tolerated (?) | soft preference       |
| Main block  | minutes × target pace     | strict      | strict        | hard required/preferred |
| Cool-down   | minutes × easy pace       | tolerated   | tolerated (?) | soft preference       |

Route shapes to support (in order of implementation effort):

1. **Segment finder** – "where near me is there ≥ N km of flat, uninterrupted tarmac?" (map layer, no routing)
2. **Out-and-back / laps** on a found segment, plus warm-up/cool-down transit from start
3. **Loop** start = end with target distance
4. **Point-to-point** (e.g. start at home, end at a train station)

### Pace model (from 5k race time)

Daniels VDOT from 5k = 18:13 → **VDOT ≈ 55.5**

| Zone       | Pace        | Used for                    |
|------------|-------------|-----------------------------|
| Easy       | 4:42–5:10/km (use 4:55) | warm-up / cool-down distance |
| Marathon   | ~4:13/km    | long-run main block         |
| Threshold  | ~3:54/km    | tempo main block            |
| Interval   | ~3:35/km    | rep lengths                 |
| 5k         | 3:39/km     | –                           |

Example: 15 min WU ≈ 3.0 km, 10 min CD ≈ 2.0 km, 25 min tempo ≈ 6.4 km.

## 2. Data

| Need             | Source                                   | Notes |
|------------------|------------------------------------------|-------|
| Road/path graph  | swisstopo **swissTLM3D** (TLM_STRASSE)   | OGD, LV95, 3D lines, has bridges/tunnels (`KUNSTBAUTE`), road class (`OBJEKTART`) |
| Surface          | swissTLM3D `BELAGSART` (hard / natural)  | Binary: cobbles/paving count as "hard". OSM `surface=*` to split asphalt vs sett/paving later |
| Elevation        | swisstopo **swissALTI3D** (0.5 m / 2 m)  | Bare-earth DTM: wrong on bridges and in tunnels/underpasses → interpolate across `KUNSTBAUTE`. Compare against TLM Z values |
| Traffic lights   | **OpenStreetMap** `highway=traffic_signals`, `crossing=traffic_signals` | **Not in swisstopo data.** Cantonal sets exist (Basel-Stadt, Luzern, …) but coverage is patchy |
| Other stops      | OSM: crossings, stop/give-way, railway level crossings, barriers, steps | |
| Traffic volume   | road class as proxy; maybe BAFU sonBASE road noise | Runner safety on roads without sidewalk |
| Nice to have     | OSM `lit`, `amenity=drinking_water`, `sidewalk` | Night runs, fountains |

Graph base: TLM. OSM *point* features are snapped to TLM nodes/edges (cheap). OSM *edge* attribute
conflation (surface, sidewalk, lit) is harder — defer.

Licensing: swisstopo OGD (attribution). OSM ODbL (attribution; share-alike if a derived database is redistributed).

## 3. Edge/node scoring (offline, Python + Polars)

- Resample each edge to ~10 m, sample DTM, smooth (e.g. 50–100 m window), handle bridges/tunnels.
- Per edge: length, ascent, descent, max sustained grade over 100/200 m, surface, road class.
- Per node: expected stop time in seconds (traffic light ≈ ½ cycle, level crossing, unsignalised crossing of major road, sharp turns).
- **Express everything as cost in seconds or s/km**, so gradient, stops and surface trade off in one unit.
- Output: GeoParquet edge/node tables + PMTiles "runnability" layer.

## 4. Routing

Off-the-shelf engines (GraphHopper, Valhalla, BRouter, OSRM) ingest OSM, not TLM, and do not support
phase-dependent cost (cost depending on distance already run). Decompose instead:

1. **Offline**: build the strict subgraph (edges/nodes passing main-block criteria); find long uninterrupted
   stretches and candidate loops / out-and-backs; index them spatially.
2. **Query time**: pick candidates whose access point lies within warm-up distance of the start; connect
   start → candidate and candidate → end with a relaxed-cost shortest path (Dijkstra/A*).
3. Rank by: main-block quality, warm-up/cool-down distance fit, total stops.

Start in Python (rustworkx / igraph). Move the loop-search kernel to Rust (PyO3/maturin) only when profiling says so.

## 5. Web app

- **v0 (static)**: MapLibre GL JS + swisstopo base map vector tiles + precomputed PMTiles runnability layer.
  Hosting on any static host. No backend. Already answers "where can I run flat?".
- **v1**: FastAPI backend holding the CH graph in memory (TLM roads fit comfortably in RAM), route
  endpoint, elevation profile, **GPX export** (for watches).
- Frontend: SvelteKit or React/Vite — thin layer around MapLibre.

## 6. Validation

- Hand-picked known-good / known-bad routes around home.
- Own GPS history (Strava/Garmin export): detect stops to calibrate traffic-light wait times and check flatness scores.

## 7. Milestones

1. Notebook for one area (e.g. city/canton of Zürich): TLM + ALTI3D grades + OSM signals → map of scored edges. Eyeball it.
2. Static runnability map (v0).
3. Segment finder + WU/CD transit routing.
4. Loop generation, GPX export, web app v1.
5. Expand to all of Switzerland, scheduled data refresh (TLM yearly, OSM weekly).

## 8. Decisions

- **OSM is fine** as a source next to swisstopo (stops, crossings, surface details).
- **Cobbles are fine** — TLM `BELAGSART = Hart` counts as tarmac.
- **Short repeats are valid results**: a 400 m track loop, or an 820 m stretch run back and forth.
  Turnarounds and sharp hairpins carry a penalty.
- **Multiple starting points**: suggestions are generated per start point; offline candidates make this cheap.
- **Warm-up / cool-down** relax gradient *and* traffic/stop penalties, with the same preferences (just weaker).
- **Main block** is defined by distance *or* duration (duration × target pace → distance).
- **No hard flatness threshold**: gradient penalty is non-linear (small grades ≈ free, grows fast).
- **Tarmac mode off** → prefer firm natural surfaces (gravel paths), not "anything goes".
- **Single user** for now; accounts, hosting, ODbL questions later.
- **Python only** for now, no Rust until clearly needed.
- **Start region: Zürich.**
- Conventional commits per step.
