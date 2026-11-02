"""Penalty model. Everything is expressed in seconds (or seconds per km) of "felt" cost.

Values are preferences, not physiology: they say how much detour a runner would accept to avoid
something. All of them are meant to be tuned.
"""

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from flatarmac.elevation import Profile


class SurfaceMode(StrEnum):
    TARMAC = "tarmac"  # hard surfaces (asphalt, concrete, cobbles)
    FIRM_NATURAL = "firm_natural"  # gravel and forest roads, woodchip tracks


class RoadClass(StrEnum):
    PATH = "path"
    MINOR = "minor"
    MEDIUM = "medium"
    MAJOR = "major"


class Control(StrEnum):
    SIGNAL = "signal"
    MARKED = "marked"
    UNMARKED = "unmarked"


PATH_OBJEKTART = {"1m Weg", "2m Weg", "1m Wegfragment", "2m Wegfragment", "Markierte Spur"}
MEDIUM_OBJEKTART = {"6m Strasse"}
MAJOR_OBJEKTART = {"8m Strasse", "10m Strasse"}
THROUGH_ROAD = {"Durchgangsstrasse", "Verbindungsstrasse", "Hochleistungsstrasse"}
STAIRS = {"Treppe", "Bruecke mit Treppe", "Unterfuehrung mit Treppe"}


def road_class(objektart: str, verkehrsbd: str | None) -> RoadClass:
    if objektart in PATH_OBJEKTART:
        return RoadClass.PATH
    if objektart in MAJOR_OBJEKTART:
        return RoadClass.MAJOR
    through = verkehrsbd in THROUGH_ROAD
    if objektart in MEDIUM_OBJEKTART:
        return RoadClass.MAJOR if through else RoadClass.MEDIUM
    return RoadClass.MEDIUM if through else RoadClass.MINOR


@dataclass(frozen=True)
class GradientPenalty:
    """Non-linear: ~free below `free_grade`, then grows with `exponent`."""

    free_grade: float = 0.002
    ref_grade: float = 0.01
    s_per_km_at_ref: float = 8.0
    exponent: float = 2.0
    # Very short edges can show implausible grades from Z noise; stairs are priced separately.
    max_grade: float = 0.25

    def s_per_km(self, grade: np.ndarray) -> np.ndarray:
        excess = np.maximum(np.minimum(np.abs(grade), self.max_grade) - self.free_grade, 0.0)
        return self.s_per_km_at_ref * (excess / self.ref_grade) ** self.exponent

    def edge_seconds(self, profile: Profile) -> float:
        segment_km = np.diff(profile.distance_m) / 1000
        return float((segment_km * self.s_per_km(profile.grade)).sum())


@dataclass(frozen=True)
class CostModel:
    gradient: GradientPenalty = field(default_factory=GradientPenalty)

    # s/km by surface mode and (belagsart, path?) — path matters for natural surfaces only.
    surface_s_per_km: dict = field(
        default_factory=lambda: {
            SurfaceMode.TARMAC: {"Hart": 0.0, "k_W": 15.0, "Natur": 40.0, "Natur_path": 40.0},
            SurfaceMode.FIRM_NATURAL: {"Hart": 10.0, "k_W": 10.0, "Natur": 0.0, "Natur_path": 15.0},
        }
    )
    # Running next to traffic: noise, busy sidewalks.
    traffic_s_per_km: dict = field(
        default_factory=lambda: {
            RoadClass.PATH: 0.0,
            RoadClass.MINOR: 2.0,
            RoadClass.MEDIUM: 6.0,
            RoadClass.MAJOR: 15.0,
        }
    )
    stairs_s: float = 300.0

    # Crossing one road at a junction, by how it is controlled.
    crossing_s: dict = field(
        default_factory=lambda: {
            Control.SIGNAL: {
                RoadClass.PATH: 0.0,
                RoadClass.MINOR: 8.0,
                RoadClass.MEDIUM: 18.0,
                RoadClass.MAJOR: 30.0,
            },
            Control.MARKED: {
                RoadClass.PATH: 0.0,
                RoadClass.MINOR: 2.0,
                RoadClass.MEDIUM: 4.0,
                RoadClass.MAJOR: 8.0,
            },
            Control.UNMARKED: {
                RoadClass.PATH: 0.5,
                RoadClass.MINOR: 2.0,
                RoadClass.MEDIUM: 8.0,
                RoadClass.MAJOR: 20.0,
            },
        }
    )
    # Stops along an edge (OSM tag value -> seconds).
    along_stop_s: dict = field(
        default_factory=lambda: {
            "level_crossing": 25.0,
            "crossing": 15.0,  # pedestrian crossing of a railway
            "tram_level_crossing": 3.0,
            "tram_crossing": 3.0,
            "gate": 4.0,
            "lift_gate": 1.0,
            "swing_gate": 3.0,
            "stile": 6.0,
            "kissing_gate": 5.0,
            "turnstile": 5.0,
            "full-height_turnstile": 6.0,
        }
    )
    # Turn penalty: free up to `free_turn_deg`, quadratic up to `u_turn_s` at 180°.
    free_turn_deg: float = 45.0
    u_turn_s: float = 5.0

    def surface_key(self, belagsart: str, rclass: RoadClass) -> str:
        if belagsart == "Natur" and rclass == RoadClass.PATH:
            return "Natur_path"
        return belagsart if belagsart in ("Hart", "Natur") else "k_W"

    def turn_s(self, deflection_deg: float) -> float:
        excess = max(deflection_deg - self.free_turn_deg, 0.0) / (180.0 - self.free_turn_deg)
        return self.u_turn_s * excess**2


@dataclass(frozen=True)
class PhaseWeights:
    """Multipliers on the penalty groups. Warm-up/cool-down keep the same preferences, weaker."""

    gradient: float = 1.0
    stops: float = 1.0
    surface: float = 1.0
    traffic: float = 1.0
    obstacles: float = 1.0


MAIN = PhaseWeights()
TRANSIT = PhaseWeights(gradient=0.15, stops=0.35, surface=0.5, traffic=0.5, obstacles=0.3)
