"""Web UI backend: static map + route planning API. Single user, runs locally."""

import json
import threading
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pyproj import Transformer

from flatarmac import layers
from flatarmac.config import LV95, PACKAGE_DIR, WGS84, Region
from flatarmac.output import write
from flatarmac.pace import MAIN_ZONES, Runner, Zone, format_pace
from flatarmac.planner import Planner, make_request

WEB_DIR = PACKAGE_DIR / "web"
RESULT_NAME = "web"
FIVE_K = r"^\d{1,2}:\d{2}$"


class PlanInput(BaseModel):
    start: tuple[float, float]  # (lat, lon)
    end: tuple[float, float] | None = None
    main_value: float = Field(25, gt=0)
    main_unit: Literal["min", "km"] = "min"
    main_zone: Literal["marathon", "threshold"] = "threshold"
    warmup_min: float = Field(15, ge=0)
    cooldown_min: float = Field(10, ge=0)
    mode: Literal["tarmac", "firm_natural"] = "tarmac"
    time: Literal["quiet", "busy"] = "busy"
    five_k: str = Field("18:13", pattern=FIVE_K)
    top: int = Field(10, ge=1, le=25)


def create_app(region: Region) -> FastAPI:
    planner = Planner(region)
    lock = threading.Lock()  # the planner keeps per-query state
    map_layers = {
        "edges": layers.to_geojson(layers.edge_scores(planner.edges)),
        "junctions": layers.to_geojson(layers.junctions(planner.nodes)),
        "tracks": layers.to_geojson(layers.tracks(planner.tracks)),
    }
    to_lv95 = Transformer.from_crs(WGS84, LV95, always_xy=True)
    app = FastAPI(title="flatarmac")

    def check_inside(point: tuple[float, float], label: str) -> None:
        x, y = to_lv95.transform(point[1], point[0])
        e0, n0, e1, n1 = region.bbox_lv95
        if not (e0 <= x <= e1 and n0 <= y <= n1):
            raise HTTPException(400, f"{label} is outside the {region.name} region")

    @app.get("/api/region")
    def region_info():
        return {"name": region.name, "bbox_wgs84": region.bbox_wgs84}

    @app.get("/api/layers/{name}")
    def layer(name: str):
        if name not in map_layers:
            raise HTTPException(404, f"no layer {name!r}")
        return Response(map_layers[name], media_type="application/geo+json")

    @app.get("/api/edges/{edge_id}")
    def edge(edge_id: int):
        """Hover details for one edge (edge ids are row positions)."""
        if not 0 <= edge_id < len(planner.edges):
            raise HTTPException(404, f"no edge {edge_id}")
        details = layers.edge_details(planner.edges.iloc[[edge_id]])
        return json.loads(details.to_json(orient="records"))[0]

    @app.get("/api/paces")
    def paces(five_k: str = Query(..., pattern=FIVE_K)):
        """Seconds per km by zone, so the form can show minutes as distances."""
        runner = Runner.from_5k(five_k)
        return {zone: runner.pace_s_per_km(zone) for zone in (Zone.EASY, *MAIN_ZONES)}

    @app.post("/api/plan")
    def plan(body: PlanInput):
        check_inside(body.start, "start")
        if body.end is not None:
            check_inside(body.end, "end")
        request = make_request(**body.model_dump())
        with lock:
            suggestions = planner.plan(request)
            path = write(suggestions, planner, request, region.dir / "routes", RESULT_NAME)
        return {
            "name": RESULT_NAME,
            "summary": {
                "main_m": round(request.main_m),
                "warmup_m": round(request.warmup_m),
                "cooldown_m": round(request.cooldown_m),
                "easy_pace": format_pace(request.easy_pace_s_per_km),
            },
            "geojson": json.loads(path.read_text()),
        }

    routes_dir = region.dir / "routes"
    routes_dir.mkdir(parents=True, exist_ok=True)
    app.mount(f"/data/{region.name}/routes", StaticFiles(directory=routes_dir), name="routes")
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
