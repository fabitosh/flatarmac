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


class TimeOfDay(StrEnum):
    QUIET = "quiet"  # early morning, evening, Sunday: night noise, off-peak traffic, few people
    BUSY = "busy"  # weekday daytime, Saturday: day noise, peak-hour traffic, full streets


class Sidewalk(StrEnum):
    YES = "yes"  # tagged on the road in OSM, or mapped separately alongside
    NO = "no"  # tagged as missing: you run on the carriageway
    PEDESTRIAN = "pedestrian"  # pedestrian street or living street
    UNKNOWN = "unknown"  # an OSM road without sidewalk tags
    NOT_A_ROAD = "not_a_road"  # no OSM road or traffic link alongside: passage, square, path


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
class RoadsidePenalty:
    """What running beside traffic costs, per time of day.

    Noise is a mild nudge; what costs is having no room to run (no sidewalk on a busy road) and
    crowds. Volumes are vehicles per hour, both directions; roads without a traffic-model link fall
    back to the width class.
    """

    # Noise (sonBASE dB along the edge): free up to `quiet_db`, then linear, capped.
    quiet_db: float = 55.0
    s_per_km_per_db: float = 0.2  # 3 s/km at 70 dB
    max_noise_s_per_km: float = 5.0
    # No sidewalk: up to this much, reached at `full_vph`.
    no_sidewalk_s_per_km: float = 15.0
    full_vph: float = 600.0
    # Sidewalk tags missing and no volume: this share of the width-class rate.
    unknown_share: float = 0.5
    # Crowds (index 0-1): pedestrian streets, station surroundings, shop and café density.
    crowd_s_per_km: dict = field(
        default_factory=lambda: {TimeOfDay.QUIET: 4.0, TimeOfDay.BUSY: 20.0}
    )
    # Crossing a road: by control, from free-flowing to `crossing_full_vph`.
    crossing_range_s: dict = field(
        default_factory=lambda: {
            Control.SIGNAL: (8.0, 30.0),
            Control.MARKED: (2.0, 8.0),
            Control.UNMARKED: (1.0, 30.0),
        }
    )
    crossing_full_vph: float = 1200.0
    # Roads without a volume in Quiet: this share of the width-class crossing cost.
    quiet_fallback_share: float = 0.5

    def noise_s_per_km(self, db: np.ndarray) -> np.ndarray:
        excess = np.nan_to_num(np.asarray(db, dtype=float) - self.quiet_db, nan=0.0)
        return np.clip(excess * self.s_per_km_per_db, 0.0, self.max_noise_s_per_km)

    def space_s_per_km(self, sidewalk, vph, class_s_per_km) -> np.ndarray:
        sidewalk = np.asarray(sidewalk, dtype=object)
        vph = np.asarray(vph, dtype=float)
        class_s_per_km = np.asarray(class_s_per_km, dtype=float)
        busy_share = np.clip(np.nan_to_num(vph, nan=0.0) / self.full_vph, 0.0, 1.0)
        no_room = np.where(np.isnan(vph), class_s_per_km, self.no_sidewalk_s_per_km * busy_share)
        unknown = np.where(
            np.isnan(vph), self.unknown_share * class_s_per_km, self.unknown_share * no_room
        )
        return np.select(
            [sidewalk == Sidewalk.NO, sidewalk == Sidewalk.UNKNOWN], [no_room, unknown], 0.0
        )

    def crowd_s_per_km_at(self, crowd: np.ndarray, time: TimeOfDay) -> np.ndarray:
        return self.crowd_s_per_km[time] * np.clip(np.asarray(crowd, dtype=float), 0.0, 1.0)

    def crossing_s(self, control: Control, vph: float, class_s: float, time: TimeOfDay) -> float:
        """Crossing one road; `class_s` is the width-class cost used when the volume is unknown."""
        if np.isnan(vph):
            return class_s * (self.quiet_fallback_share if time == TimeOfDay.QUIET else 1.0)
        low, high = self.crossing_range_s[control]
        return low + (high - low) * min(vph / self.crossing_full_vph, 1.0)


@dataclass(frozen=True)
class CostModel:
    gradient: GradientPenalty = field(default_factory=GradientPenalty)
    roadside: RoadsidePenalty = field(default_factory=RoadsidePenalty)

    # s/km by surface mode and (belagsart, path?) — path matters for natural surfaces only.
    surface_s_per_km: dict = field(
        default_factory=lambda: {
            SurfaceMode.TARMAC: {"Hart": 0.0, "k_W": 15.0, "Natur": 40.0, "Natur_path": 40.0},
            SurfaceMode.FIRM_NATURAL: {"Hart": 10.0, "k_W": 10.0, "Natur": 0.0, "Natur_path": 15.0},
        }
    )
    # Width-class fallback for roads without traffic or sidewalk data (see RoadsidePenalty).
    traffic_s_per_km: dict = field(
        default_factory=lambda: {
            RoadClass.PATH: 0.0,
            RoadClass.MINOR: 2.0,
            RoadClass.MEDIUM: 6.0,
            RoadClass.MAJOR: 15.0,
        }
    )
    stairs_s: float = 300.0

    # Crossing one road at a junction, by how it is controlled: width-class fallback for roads
    # without a traffic volume (see RoadsidePenalty.crossing_s).
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
    # Turn penalty: free up to `free_turn_deg`, then grows to `u_turn_s` at 180°. Breaking rhythm
    # is what makes short laps tiring: ~3 s for a 90° corner, 12 s for a turnaround.
    free_turn_deg: float = 30.0
    u_turn_s: float = 12.0
    turn_exponent: float = 1.5
    # Repeating the same stretch gets monotonous: every lap costs this much on top of its corners.
    lap_s: float = 6.0

    def surface_key(self, belagsart: str, rclass: RoadClass) -> str:
        if belagsart == "Natur" and rclass == RoadClass.PATH:
            return "Natur_path"
        return belagsart if belagsart in ("Hart", "Natur") else "k_W"

    def turn_s(self, deflection_deg: float) -> float:
        excess = max(deflection_deg - self.free_turn_deg, 0.0) / (180.0 - self.free_turn_deg)
        return self.u_turn_s * excess**self.turn_exponent


@dataclass(frozen=True)
class PhaseWeights:
    """Multipliers on the penalty groups. Warm-up/cool-down keep the same preferences, weaker."""

    gradient: float = 1.0
    stops: float = 1.0
    surface: float = 1.0
    traffic: float = 1.0
    obstacles: float = 1.0


MAIN = PhaseWeights()
TRANSIT = PhaseWeights(gradient=0.03, stops=0.35, surface=0.5, traffic=0.5, obstacles=0.3)


def edge_penalty_s(
    edges,
    mode: SurfaceMode,
    weights: PhaseWeights,
    time: TimeOfDay = TimeOfDay.BUSY,
    include_stops: bool = True,
):
    """Weighted penalty seconds per edge (excluding junction / turn costs)."""
    total = (
        weights.gradient * edges["gradient_s"]
        + weights.surface * edges[f"surface_s_{mode}"]
        + weights.traffic * edges[f"traffic_s_{time}"]
        + weights.obstacles * edges["obstacle_s"]
    )
    if include_stops:
        total = total + weights.stops * edges["stops_s"]
    return total


# Display only: penalties stay in seconds internally. 100 = no penalty at all; the score halves
# at SCORE_HALF_S_PER_KM and keeps falling (never reaching 0) beyond it.
SCORE_HALF_S_PER_KM = 50.0


def score_0_100(s_per_km):
    """Runnability score from penalty seconds per km."""
    return 100.0 / (1.0 + np.maximum(s_per_km, 0.0) / SCORE_HALF_S_PER_KM)
