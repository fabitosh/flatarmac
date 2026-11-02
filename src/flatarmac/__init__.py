import argparse

from flatarmac.config import REGIONS


def main() -> None:
    parser = argparse.ArgumentParser(prog="flatarmac")
    parser.add_argument("command", choices=["fetch", "build", "export", "all"])
    parser.add_argument("--region", default="zurich", choices=sorted(REGIONS))
    args = parser.parse_args()
    region = REGIONS[args.region]

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
