"""Training paces derived from a 5k race time (Daniels/Gilbert VDOT model)."""

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np


class Zone(StrEnum):
    EASY = "easy"
    MARATHON = "marathon"
    THRESHOLD = "threshold"
    INTERVAL = "interval"


# Paces a continuous main block can be run at (intervals need reps, easy runs need no block).
MAIN_ZONES = (Zone.MARATHON, Zone.THRESHOLD)

# Fraction of VDOT each zone runs at. Easy uses the middle of the 62-70 % band.
ZONE_INTENSITY = {
    Zone.EASY: 0.66,
    Zone.MARATHON: 0.80,
    Zone.THRESHOLD: 0.88,
    Zone.INTERVAL: 0.975,
}


def _vo2_at(speed_m_per_min: float) -> float:
    return -4.60 + 0.182258 * speed_m_per_min + 0.000104 * speed_m_per_min**2


def _fraction_sustainable(duration_min: float) -> float:
    return (
        0.8
        + 0.1894393 * math.exp(-0.012778 * duration_min)
        + 0.2989558 * math.exp(-0.1932605 * duration_min)
    )


def _speed_at_vo2(vo2: float) -> float:
    a, b, c = 0.000104, 0.182258, -4.60 - vo2
    return (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)


def parse_duration(text: str) -> float:
    """Parse "mm:ss" or "h:mm:ss" into seconds."""
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


@dataclass(frozen=True)
class Runner:
    vdot: float

    @classmethod
    def from_5k(cls, race_time: str) -> "Runner":
        minutes = parse_duration(race_time) / 60
        speed = 5000 / minutes
        return cls(vdot=_vo2_at(speed) / _fraction_sustainable(minutes))

    def pace_s_per_km(self, zone: Zone) -> float:
        speed = _speed_at_vo2(self.vdot * ZONE_INTENSITY[zone])
        return 1000 / speed * 60

    def distance_m(self, duration_s: float, zone: Zone) -> float:
        return duration_s / self.pace_s_per_km(zone) * 1000


# Grade-adjusted distance: an easy pace on the flat covers less ground uphill and a little more
# downhill. Each metre climbed costs about as much as GAP_UP_M of flat running; each metre
# descended gives back GAP_DOWN_M (steep descents slow you down again, hence the floor).
GAP_UP_M = 5.0
GAP_DOWN_M = 1.5
GAP_MIN_FACTOR = 0.5


def flat_equivalent_m(length_m, ascent_m, descent_m):
    """Distance on the flat that takes as long as running `length_m` with this climb and descent."""
    return np.maximum(
        length_m + GAP_UP_M * ascent_m - GAP_DOWN_M * descent_m, GAP_MIN_FACTOR * length_m
    )


def format_pace(s_per_km: float) -> str:
    minutes, seconds = divmod(round(s_per_km), 60)
    return f"{minutes}:{seconds:02d}/km"
