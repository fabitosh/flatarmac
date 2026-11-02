import argparse
import re

from flatarmac.config import REGIONS


def _lat_lon(text: str) -> tuple[float, float]:
    lat, lon = (float(v) for v in text.split(","))
    return lat, lon


def _main_block_m(text: str, runner, zone) -> float:
    """ "8km", "8000m" or "25min" (duration at the main-block zone pace)."""
    match = re.fullmatch(r"([\d.]+)\s*(km|m|min)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"cannot parse main block {text!r}")
    value, unit = float(match[1]), match[2]
    if unit == "min":
        return runner.distance_m(value * 60, zone)
    return value * 1000 if unit == "km" else value


def _plan(args, region) -> None:
    from flatarmac.costs import SurfaceMode
    from flatarmac.output import write
    from flatarmac.pace import Runner, Zone, format_pace
    from flatarmac.planner import Planner, Request

    runner = Runner.from_5k(args.five_k)
    main_m = _main_block_m(args.main, runner, Zone(args.main_zone))
    easy = runner.pace_s_per_km(Zone.EASY)
    request = Request(
        start=args.start,
        end=args.end,
        main_m=main_m,
        warmup_m=runner.distance_m(args.warmup * 60, Zone.EASY),
        cooldown_m=runner.distance_m(args.cooldown * 60, Zone.EASY),
        easy_pace_s_per_km=easy,
        mode=SurfaceMode(args.mode),
        top=args.top,
    )
    planner = Planner(region)
    suggestions = planner.plan(request)
    path = write(suggestions, planner, request, region.dir / "routes", args.name)

    print(
        f"main {main_m / 1000:.1f} km · warm-up {request.warmup_m / 1000:.1f} km · "
        f"cool-down {request.cooldown_m / 1000:.1f} km · easy {format_pace(easy)}"
    )
    for rank, s in enumerate(suggestions, start=1):
        print(
            f"{rank}. {s.name} [{s.kind}] lap {s.lap_m:.0f} m × {s.laps:.1f} · "
            f"main {s.main_s / (main_m / 1000):.1f} s/km · "
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

    plan = commands.add_parser("plan", help="suggest routes from a start point")
    plan.add_argument("--start", type=_lat_lon, required=True, help="lat,lon")
    plan.add_argument("--end", type=_lat_lon, help="lat,lon (default: back to start)")
    plan.add_argument("--main", default="8km", help='"8km", "8000m" or "25min"')
    plan.add_argument("--main-zone", default="threshold", help="pace zone for --main in minutes")
    plan.add_argument("--warmup", type=float, default=15, help="minutes")
    plan.add_argument("--cooldown", type=float, default=10, help="minutes")
    plan.add_argument("--mode", default="tarmac", choices=["tarmac", "firm_natural"])
    plan.add_argument("--five-k", default="18:13")
    plan.add_argument("--top", type=int, default=5)
    plan.add_argument("--name", default="latest", help="output file name")

    args = parser.parse_args()
    region = REGIONS[args.region]

    if args.command == "plan":
        _plan(args, region)
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
