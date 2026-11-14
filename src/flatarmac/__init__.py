import argparse
import re
import sys
import threading
import time
import webbrowser

from flatarmac.config import (
    DATA_DIR,
    LV95,
    MAX_RADIUS_KM,
    WGS84,
    Region,
    curated_path,
    regions,
)


def _lat_lon(text: str) -> tuple[float, float]:
    lat, lon = (float(v) for v in text.split(","))
    return lat, lon


def _main_block(text: str) -> tuple[float, str]:
    """ "8km", "8000m" or "25min"."""
    match = re.fullmatch(r"([\d.]+)\s*(km|m|min)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"cannot parse main block {text!r}")
    return float(match[1]), match[2]


def _osm_ref(text: str) -> tuple[str, int]:
    """ "relation/123" or "way/456"."""
    osm_type, osm_id = text.split("/")
    return osm_type, int(osm_id)


def _pick_region(parser: argparse.ArgumentParser, name: str | None) -> Region:
    """--region if given; else the only region set up (or the only one known)."""
    known = regions()
    if name:
        if name not in known:
            parser.error(f"unknown region {name!r}; known: {', '.join(known)}")
        return known[name]
    built = [r for r in known.values() if r.is_built] or list(known.values())
    if len(built) > 1:
        parser.error(f"several regions, pick one with --region: {', '.join(r.name for r in built)}")
    return built[0]


def _setup_region(parser: argparse.ArgumentParser, args) -> Region:
    """Region around a place name or "lat,lon", saved so later commands find it."""
    from pyproj import Transformer

    from flatarmac import places

    if re.fullmatch(r"\s*-?[\d.]+\s*,\s*-?[\d.]+\s*", args.place):
        if not args.name:
            parser.error("give the region a --name when setting it up around coordinates")
        lat, lon = _lat_lon(args.place)
        east, north = Transformer.from_crs(WGS84, LV95, always_xy=True).transform(lon, lat)
        city = ""
    else:
        east, north, city = places.lookup(args.place)
        print(f"found {city} at {east:.0f}, {north:.0f} (LV95)")
        # The search is fuzzy: let a person check the match before a long download.
        if sys.stdin.isatty() and input(f"set up {city}? [Y/n] ").strip().lower() not in ("", "y"):
            parser.exit(message="nothing set up\n")
    region = Region.around(args.name or places.slug(city), east, north, args.radius, city)
    # Same name and bounds resumes an interrupted setup; other bounds would mix two areas' data.
    existing = regions().get(region.name)
    if existing and existing.bbox_lv95 != region.bbox_lv95:
        parser.error(f"a region {region.name!r} with other bounds exists: pick another --name")
    region.save()
    return region


def _step(label: str, func, *args) -> None:
    print(f"{label}...", flush=True)
    started = time.monotonic()
    func(*args)
    print(f"{label}: done in {time.monotonic() - started:.0f} s", flush=True)


def _fetch(region: Region, keep_existing: bool = False) -> None:
    """Download every source; with `keep_existing`, skip those already fetched (resumes setup)."""
    from flatarmac.sources import alti3d, noise, osm, tlm, traffic

    sources = [
        ("roads (swissTLM3D, ~0.9 GB once for all regions)", tlm.fetch, "tlm_roads.parquet"),
        ("elevation (swissALTI3D)", alti3d.fetch, "dem_2m.tif"),
        ("OpenStreetMap stops, tracks and sidewalks", osm.fetch, "osm_walk.parquet"),
        ("road noise", noise.fetch, "noise_night.tif"),
        ("traffic volumes", traffic.fetch, "traffic_links.parquet"),
    ]
    for label, fetch, last_output in sources:
        if keep_existing and (region.dir / last_output).exists():
            print(f"{label}: already fetched")
        else:
            _step(label, fetch, region)


def _build(region: Region) -> None:
    from flatarmac.build import build

    _step("network and costs", build, region)


def _plan(args, region) -> None:
    from flatarmac.output import route_score, write
    from flatarmac.pace import format_pace
    from flatarmac.planner import Planner, make_request

    main_value, main_unit = args.main
    request = make_request(
        start=args.start,
        end=args.end,
        main_value=main_value,
        main_unit=main_unit,
        main_zone=args.main_zone,
        warmup_min=args.warmup,
        cooldown_min=args.cooldown,
        five_k=args.five_k,
        mode=args.mode,
        time=args.time,
        top=args.top,
    )
    planner = Planner(region)
    suggestions = planner.plan(request)
    path = write(suggestions, planner, request, region.dir / "routes", args.name)

    print(
        f"main {request.main_m / 1000:.1f} km · warm-up {request.warmup_m / 1000:.1f} km · "
        f"cool-down {request.cooldown_m / 1000:.1f} km · "
        f"easy {format_pace(request.easy_pace_s_per_km)}"
    )
    for rank, s in enumerate(suggestions, start=1):
        print(
            f"{rank}. {s.name} [{s.kind}] lap {s.lap_m:.0f} m × {s.laps:.1f} · "
            f"to block {s.warmup_m / 1000:.1f} km, back {s.cooldown_m / 1000:.1f} km · "
            f"score {route_score(s, request):.0f} ({s.score:.0f} s)"
        )
    print(f"written: {path} (+ GPX per suggestion)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="flatarmac")
    parser.add_argument("--region", help="default: the only region set up")
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser(
        "setup", help="download and build a region around a place (fetch, tracks, build)"
    )
    setup.add_argument("place", help='place name ("Bern", "Zürich Oerlikon", "8400") or lat,lon')
    setup.add_argument(
        "--radius", type=float, default=8, help=f"km around the place, at most {MAX_RADIUS_KM}"
    )
    setup.add_argument("--name", help="region name (default: from the place)")

    download = commands.add_parser(
        "download", help="install a prebuilt region from the latest release instead of `setup`"
    )
    download.add_argument("name", nargs="?", default="zurich", help="default: zurich")
    commands.add_parser("bundle", help="pack a built region for a release (into <data>/dist)")
    for name in ("fetch", "build", "all"):
        commands.add_parser(name)

    tracks = commands.add_parser(
        "tracks", help="add newly found tracks to tracks_<region>.csv (after fetch)"
    )
    tracks.add_argument("--discover", action="store_true", help="query Overpass for tracks first")
    tracks.add_argument("--add", type=_osm_ref, action="append", default=[], help="relation/123")

    plan = commands.add_parser("plan", help="suggest routes from a start point")
    plan.add_argument("--start", type=_lat_lon, required=True, help="lat,lon")
    plan.add_argument("--end", type=_lat_lon, help="lat,lon (default: back to start)")
    plan.add_argument("--main", type=_main_block, default="8km", help='"8km", "8000m", "25min"')
    plan.add_argument(
        "--main-zone",
        default="threshold",
        choices=["marathon", "threshold"],
        help="pace zone for --main in minutes",
    )
    plan.add_argument("--warmup", type=float, default=15, help="minutes")
    plan.add_argument("--cooldown", type=float, default=10, help="minutes")
    plan.add_argument("--mode", default="tarmac", choices=["tarmac", "firm_natural"])
    plan.add_argument(
        "--time",
        default="busy",
        choices=["quiet", "busy"],
        help="quiet: early, evening or Sunday; busy: weekday daytime or Saturday",
    )
    plan.add_argument("--five-k", default="18:13")
    plan.add_argument(
        "--top",
        type=int,
        default=10,
        help="non-track suggestions; feasible tracks are always added",
    )
    plan.add_argument("--name", default="latest", help="output file name")

    serve = commands.add_parser("serve", help="web UI with route planning")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true", help="do not open a browser tab")

    args = parser.parse_args()
    if args.command == "setup":
        from flatarmac import tracklist

        region = _setup_region(parser, args)
        _fetch(region, keep_existing=True)
        if not curated_path("tracks", region).exists():
            _step("running tracks", tracklist.refresh, region)
        _build(region)
        print(f"{region.name} is ready: run `flatarmac serve` to plan routes")
        return
    if args.command == "download":
        from flatarmac import bundles

        region = bundles.install(args.name)
        print(f"{region.name} is ready: run `flatarmac serve` to plan routes")
        return
    region = _pick_region(parser, args.region)
    if args.command == "bundle":
        from flatarmac import bundles

        path = bundles.pack(region, DATA_DIR / "dist")
        print(f"written: {path} ({path.stat().st_size / 1e6:.0f} MB)")
        return

    if args.command == "plan":
        _plan(args, region)
        return
    if args.command == "tracks":
        from flatarmac import tracklist
        from flatarmac.sources import osm

        if args.discover:
            osm.fetch_tracks(region)
        tracklist.refresh(region, args.add)
        print(f"updated {tracklist.csv_path(region)}; run `flatarmac build` to use it")
        return
    if args.command == "serve":
        import uvicorn

        from flatarmac.server import create_app

        if not region.is_built:
            parser.error(
                f"{region.name} is not built yet: run `flatarmac download` (greater Zürich) "
                "or `flatarmac setup <place>` first"
            )
        app = create_app(region)
        url = f"http://localhost:{args.port}"
        print(f"flatarmac for {region.name} at {url} (Ctrl+C to stop)")
        if not args.no_browser:
            threading.Timer(1.0, webbrowser.open, [url]).start()
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        return
    if args.command in ("fetch", "all"):
        _fetch(region)
    if args.command in ("build", "all"):
        _build(region)
