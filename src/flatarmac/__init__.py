import argparse
import re

from flatarmac.config import REGIONS


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


def _plan(args, region) -> None:
    from flatarmac.output import write
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
        top=args.top,
    )
    planner = Planner(region)
    suggestions = planner.plan(request)
    path = write(suggestions, planner, request, region.dir / "routes", args.name)

    main_km = request.main_m / 1000
    print(
        f"main {main_km:.1f} km · warm-up {request.warmup_m / 1000:.1f} km · "
        f"cool-down {request.cooldown_m / 1000:.1f} km · "
        f"easy {format_pace(request.easy_pace_s_per_km)}"
    )
    for rank, s in enumerate(suggestions, start=1):
        print(
            f"{rank}. {s.name} [{s.kind}] lap {s.lap_m:.0f} m × {s.laps:.1f} · "
            f"main {s.main_s / main_km:.1f} s/km · "
            f"to block {s.warmup_m / 1000:.1f} km, back {s.cooldown_m / 1000:.1f} km · "
            f"score {s.score:.0f}"
        )
    print(f"written: {path} (+ GPX per suggestion)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="flatarmac")
    parser.add_argument("--region", default="zurich", choices=sorted(REGIONS))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch", "build", "export", "all"):
        commands.add_parser(name)

    tracks = commands.add_parser(
        "tracks", help="add newly found tracks to curated/tracks_<region>.csv (after build)"
    )
    tracks.add_argument("--discover", action="store_true", help="query Overpass for tracks first")
    tracks.add_argument("--add", type=_osm_ref, action="append", default=[], help="relation/123")

    plan = commands.add_parser("plan", help="suggest routes from a start point")
    plan.add_argument("--start", type=_lat_lon, required=True, help="lat,lon")
    plan.add_argument("--end", type=_lat_lon, help="lat,lon (default: back to start)")
    plan.add_argument("--main", type=_main_block, default="8km", help='"8km", "8000m", "25min"')
    plan.add_argument("--main-zone", default="threshold", help="pace zone for --main in minutes")
    plan.add_argument("--warmup", type=float, default=15, help="minutes")
    plan.add_argument("--cooldown", type=float, default=10, help="minutes")
    plan.add_argument("--mode", default="tarmac", choices=["tarmac", "firm_natural"])
    plan.add_argument("--five-k", default="18:13")
    plan.add_argument("--top", type=int, default=10)
    plan.add_argument("--name", default="latest", help="output file name")

    serve = commands.add_parser("serve", help="web UI with route planning")
    serve.add_argument("--port", type=int, default=8765)

    args = parser.parse_args()
    region = REGIONS[args.region]

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

        uvicorn.run(create_app(region), host="127.0.0.1", port=args.port)
        return
    if args.command in ("fetch", "all"):
        from flatarmac.sources import alti3d, osm, tlm

        tlm.fetch(region)
        alti3d.fetch(region)
        osm.fetch(region)
    if args.command in ("build", "all"):
        from flatarmac.build import build

        build(region)
    if args.command in ("export", "all"):
        from flatarmac.export import export

        export(region)
