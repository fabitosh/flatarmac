"""Web UI backend: static map + route planning API. Single user, runs locally."""

import json
import threading
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pyproj import Transformer

from flatarmac.config import DATA_DIR, LV95, WGS84, Region
from flatarmac.output import write
from flatarmac.pace import format_pace
from flatarmac.planner import Planner, make_request

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
RESULT_NAME = "web"


class PlanInput(BaseModel):
    start: tuple[float, float]  # (lat, lon)
    end: tuple[float, float] | None = None
    main_value: float = Field(25, gt=0)
    main_unit: Literal["min", "km"] = "min"
    main_zone: Literal["easy", "marathon", "threshold", "interval"] = "threshold"
    warmup_min: float = Field(15, ge=0)
    cooldown_min: float = Field(10, ge=0)
    mode: Literal["tarmac", "firm_natural"] = "tarmac"
    five_k: str = Field("18:13", pattern=r"^\d{1,2}:\d{2}$")
    top: int = Field(10, ge=1, le=25)


def create_app(region: Region) -> FastAPI:
    planner = Planner(region)
    lock = threading.Lock()  # the planner keeps per-query state
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

    app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
