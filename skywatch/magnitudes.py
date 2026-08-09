"""How bright a satellite looks from the ground.

The standard model, as used by Quicksat and Stellarium:

    mag = std_mag - 15.75 + 2.5 * log10(range_km**2 / illuminated_fraction)

`std_mag` is the object's brightness at 1000 km range, 50% illuminated, so the
constants cancel at that reference point. On top of that we add atmospheric
extinction, which matters a lot: a pass at 15 deg elevation loses roughly a full
magnitude to air, and most predictors skip it.

Sanity checks against reality:
  ISS (std -2.0) overhead at 400 km   -> -4.0   (observed max is about -3.9)
  fresh Starlink (std 4.0) at 350 km  ->  1.7   (trains are reported at 1-3)
  operational Starlink at 550 km      ->  4.6   (reported "barely visible")
"""
from __future__ import annotations

import math

# Extinction coefficient in magnitudes per airmass at sea level, clear sky.
EXTINCTION_K = 0.28

# Starlink is absent from McCants' file, so model it by mission phase.
# Standard magnitudes (1000 km, 50% illuminated):
STARLINK_STD_MAG = {
    # Freshly deployed and still raising orbit. Not yet in the operational
    # "shark-fin" attitude, so the flat body catches the sun broadside -- this
    # is the phase that produces the naked-eye string of pearls.
    "raising": 4.0,
    # On station. Sunshade/dielectric mirror film applied.
    "operational": 5.9,
}
STARLINK_RAISING_BELOW_KM = 480.0

# Reasonable fallback for an object with no measured magnitude: treat it as a
# diffuse sphere of the given radar cross section. Deliberately conservative --
# better to under-promise a pass than to alert on something invisible.
RCS_FALLBACK_ALBEDO = 0.20


def airmass(altitude_deg: float) -> float:
    """Pickering (2002). Well behaved all the way down to the horizon."""
    h = max(0.0, altitude_deg)
    denom = math.sin(math.radians(h + 244.0 / (165.0 + 47.0 * h ** 1.1)))
    if denom <= 1e-6:
        return 40.0
    return 1.0 / denom


def extinction(altitude_deg: float) -> float:
    """Magnitudes lost to the atmosphere at this elevation."""
    return EXTINCTION_K * min(airmass(altitude_deg), 12.0)


def illuminated_fraction(phase_angle_rad: float) -> float:
    """Fraction of the disc lit, as seen by the observer. 1.0 = fully lit."""
    return (1.0 + math.cos(phase_angle_rad)) / 2.0


def apparent_magnitude(std_mag: float, range_km: float, phase_angle_rad: float,
                       altitude_deg: float) -> float | None:
    frac = illuminated_fraction(phase_angle_rad)
    if frac <= 0.002 or range_km <= 0:
        return None
    mag = std_mag - 15.75 + 2.5 * math.log10(range_km * range_km / frac)
    return mag + extinction(altitude_deg)


def std_mag_from_rcs(rcs_m2: float) -> float:
    """Very rough standard magnitude from radar cross section.

    Treats the object as a Lambertian sphere of the same projected area. Real
    satellites are neither spherical nor grey, so this is only used when nothing
    better exists, and callers should treat the result as a lower bound on how
    faint the object is.
    """
    if not rcs_m2 or rcs_m2 <= 0:
        return 8.0
    radius_m = math.sqrt(rcs_m2 / math.pi)
    # Brightness goes with projected area, so magnitude goes with -5*log10(radius).
    # Anchored so a 1 m^2 RCS object lands near magnitude 6.5 at the reference
    # geometry, which matches the faint end of McCants' catalogue.
    reference_radius = math.sqrt(1.0 / math.pi)
    return 6.5 - 5.0 * math.log10(max(radius_m, 0.05) / reference_radius)


class MagnitudeModel:
    """Resolves a standard magnitude for any object we might report."""

    def __init__(self, std_mags: dict[int, float], rcs: dict[int, float] | None = None):
        self.std_mags = std_mags
        self.rcs = rcs or {}

    def standard_magnitude(self, norad: int, name: str, altitude_km: float) -> tuple[float, str]:
        """Return (std_mag, source) for one object."""
        if name.upper().startswith("STARLINK"):
            phase = "raising" if altitude_km < STARLINK_RAISING_BELOW_KM else "operational"
            return STARLINK_STD_MAG[phase], f"starlink:{phase}"
        known = self.std_mags.get(norad)
        if known is not None:
            return known, "mccants"
        rcs = self.rcs.get(norad)
        if rcs:
            return std_mag_from_rcs(rcs), "rcs"
        return 8.0, "unknown"

    def brightness_word(self, mag: float) -> str:
        if mag <= -2.5:
            return "spectacular"
        if mag <= -1.0:
            return "very bright"
        if mag <= 1.0:
            return "bright"
        if mag <= 2.5:
            return "easy to see"
        if mag <= 3.5:
            return "visible"
        if mag <= 4.5:
            return "faint"
        return "very faint"
