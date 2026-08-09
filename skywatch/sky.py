"""Turning numbers into things a person standing outside can act on."""
from __future__ import annotations

import math

COMPASS_16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

# Arrows point the way you turn to face that bearing, drawn as if north is up.
COMPASS_ARROW = {
    "N": "↑", "NNE": "↑", "NE": "↗", "ENE": "↗",
    "E": "→", "ESE": "→", "SE": "↘", "SSE": "↘",
    "S": "↓", "SSW": "↓", "SW": "↙", "WSW": "↙",
    "W": "←", "WNW": "←", "NW": "↖", "NNW": "↖",
}


def compass(azimuth_deg: float) -> str:
    """Azimuth in degrees (0 = north, clockwise) -> 16-point compass name."""
    return COMPASS_16[int((azimuth_deg % 360) / 22.5 + 0.5) % 16]


def arrow(azimuth_deg: float) -> str:
    return COMPASS_ARROW[compass(azimuth_deg)]


def elevation_hint(altitude_deg: float) -> str:
    """Plain-language description of how high to look.

    Calibrated on the fist-at-arm's-length trick: a clenched fist held at arm's
    length spans about 10 degrees, which is the only measuring tool anyone
    actually has outdoors.
    """
    a = altitude_deg
    if a < 15:
        return "just above the rooftops"
    if a < 25:
        return "two fists above the horizon"
    if a < 40:
        return "a third of the way up"
    if a < 55:
        return "halfway up the sky"
    if a < 75:
        return "high up, most of the way to overhead"
    return "almost straight overhead"


def fists(altitude_deg: float) -> int:
    """How many fists at arm's length above the horizon."""
    return max(1, round(altitude_deg / 10.0))


def angular_separation(az1: float, alt1: float, az2: float, alt2: float) -> float:
    """Great-circle angle between two points on the sky, in degrees."""
    a1, a2 = math.radians(alt1), math.radians(alt2)
    d_az = math.radians(az1 - az2)
    c = math.sin(a1) * math.sin(a2) + math.cos(a1) * math.cos(a2) * math.cos(d_az)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def track_line(az_start: float, az_peak: float, az_end: float) -> str:
    """Compact 'where it travels' string, e.g. 'WSW -> S -> ENE'."""
    parts = [compass(az_start), compass(az_peak), compass(az_end)]
    dedup = [parts[0]]
    for p in parts[1:]:
        if p != dedup[-1]:
            dedup.append(p)
    return " → ".join(dedup)


def sky_diagram(az_start: float, alt_start: float,
                az_peak: float, alt_peak: float,
                az_end: float, alt_end: float) -> str:
    """A tiny fixed-width sky chart: you at the centre, horizon at the edge.

    Zenith-centred polar projection, so the middle of the box is straight up and
    the border is the horizon. North is at the top and east is on the LEFT, which
    looks backwards on paper but is correct for a chart you hold up and look
    through -- the same convention every planisphere uses.

    Track markers: o where it appears, * highest point, x where it goes.
    """
    W, H = 25, 13
    grid = [[" "] * W for _ in range(H)]
    cx, cy = W // 2, H // 2

    def plot(az: float, alt: float, ch: str, overwrite: bool = True) -> None:
        r = (90.0 - max(0.0, min(90.0, alt))) / 90.0
        th = math.radians(az)
        x = int(round(cx - r * cx * math.sin(th)))
        y = int(round(cy - r * cy * math.cos(th)))
        if 0 <= x < W and 0 <= y < H and (overwrite or grid[y][x] == " "):
            grid[y][x] = ch

    for deg in range(0, 360, 3):
        plot(deg, 0.0, ".")
    # the pass itself, interpolated through the three known points
    for t in range(51):
        f = t / 50.0
        if f <= 0.5:
            g = f * 2
            az = _interp_az(az_start, az_peak, g)
            alt = alt_start + (alt_peak - alt_start) * g
        else:
            g = (f - 0.5) * 2
            az = _interp_az(az_peak, az_end, g)
            alt = alt_peak + (alt_end - alt_peak) * g
        plot(az, alt, "·")
    # Drawn least-to-most important, because two of these can land on the same
    # cell: a satellite leaving Earth's shadow at its highest point has start ==
    # peak, and one entering shadow at the top has end == peak. Whichever is
    # drawn last survives, and "where it appears" is what you need first.
    plot(az_peak, alt_peak, "*")
    plot(az_end, alt_end, "x")
    plot(az_start, alt_start, "o")
    if grid[cy][cx] == " ":
        grid[cy][cx] = "+"

    lines = ["".join(row) for row in grid]
    # Cardinal labels sit outside the ring so they cannot be mistaken for track
    # markers. East is on the left because you are looking up, not down.
    lines[cy] = "E " + lines[cy][2:-2] + " W"
    out = ["".join(" " * ((W - 1) // 2) + "N")] + lines + ["".join(" " * ((W - 1) // 2) + "S")]
    return "\n".join(line.rstrip() for line in out)


def _interp_az(a: float, b: float, f: float) -> float:
    """Interpolate azimuth the short way around the circle."""
    d = ((b - a + 180) % 360) - 180
    return (a + d * f) % 360
