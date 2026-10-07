"""Orbit propagation: OMM/TLE elements -> SGP4 -> latitude / longitude / altitude.

SGP4 produces positions in the TEME (True Equator, Mean Equinox) frame.
We rotate TEME -> Earth-fixed using Greenwich Mean Sidereal Time and then
convert Earth-fixed Cartesian coordinates to WGS-84 geodetic coordinates.
Polar motion and the equation of the equinoxes are ignored; the resulting
error (a few hundred metres at most) is invisible on a world map and far
below the intrinsic error of SGP4 itself (~1 km at epoch, growing with time).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from sgp4 import omm
from sgp4.api import Satrec, SatrecArray, jday

# WGS-84 ellipsoid
_A = 6378.137  # km, equatorial radius
_F = 1.0 / 298.257223563
_E2 = _F * (2.0 - _F)


@dataclass
class Satellite:
    norad_id: int
    name: str
    epoch: datetime  # element-set epoch (UTC)
    satrec: Satrec


def satellite_from_gp(record: dict) -> Satellite:
    """Build a Satellite from one Space-Track GP (OMM-in-JSON) record."""
    sat = Satrec()
    try:
        omm.initialize(sat, record)
    except Exception:
        # Fall back to the classic two-line element set
        sat = Satrec.twoline2rv(record["TLE_LINE1"], record["TLE_LINE2"])
    epoch = datetime.fromisoformat(record["EPOCH"]).replace(tzinfo=timezone.utc)
    return Satellite(
        norad_id=int(record["NORAD_CAT_ID"]),
        name=record.get("OBJECT_NAME", str(record["NORAD_CAT_ID"])).strip(),
        epoch=epoch,
        satrec=sat,
    )


def gmst(jd_ut1: np.ndarray) -> np.ndarray:
    """Greenwich Mean Sidereal Time (IAU-82, as used with TEME), radians."""
    t = (jd_ut1 - 2451545.0) / 36525.0
    seconds = (
        -6.2e-6 * t**3
        + 0.093104 * t**2
        + (876600.0 * 3600.0 + 8640184.812866) * t
        + 67310.54841
    )
    return np.mod(np.radians(seconds / 240.0), 2.0 * np.pi)


def ecef_to_geodetic(x, y, z):
    """Earth-fixed Cartesian (km) -> WGS-84 latitude/longitude (deg), height (km)."""
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1.0 - _E2))
    for _ in range(6):  # converges to < 1 mm for any Earth orbit
        sin_lat = np.sin(lat)
        n = _A / np.sqrt(1.0 - _E2 * sin_lat**2)
        lat = np.arctan2(z + _E2 * n * sin_lat, p)
    sin_lat, cos_lat = np.sin(lat), np.cos(lat)
    # height formula that stays well-defined over the poles
    h = p * cos_lat + z * sin_lat - _A * np.sqrt(1.0 - _E2 * sin_lat**2)
    return np.degrees(lat), np.degrees(lon), h


def _julian(times: list[datetime]):
    jd = np.empty(len(times))
    fr = np.empty(len(times))
    for i, t in enumerate(times):
        t = t.astimezone(timezone.utc)
        jd[i], fr[i] = jday(
            t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond * 1e-6
        )
    return jd, fr


def _propagate_raw(sats: list[Satellite], times: list[datetime]):
    """SGP4 for many satellites x times. Returns err, TEME r, Earth-fixed (x, y, z), jd."""
    jd, fr = _julian(times)
    arr = SatrecArray([s.satrec for s in sats])
    err, r, v = arr.sgp4(jd, fr)  # r, v: (n_sats, n_times, 3) km, km/s in TEME

    theta = gmst(jd + fr)  # (n_times,)
    c, s = np.cos(theta), np.sin(theta)
    x = c * r[..., 0] + s * r[..., 1]
    y = -s * r[..., 0] + c * r[..., 1]
    z = r[..., 2]
    return err, r, v, (x, y, z), jd + fr


def propagate(sats: list[Satellite], times: list[datetime]):
    """Propagate many satellites to many times in one vectorised call.

    Returns dict of arrays shaped (n_sats, n_times): lat, lon, alt_km,
    speed_kms and ok (False where SGP4 reported an error, e.g. decayed).
    """
    if not sats or not times:
        empty = np.empty((len(sats), len(times)))
        return {"lat": empty, "lon": empty, "alt_km": empty, "speed_kms": empty,
                "ok": empty.astype(bool)}

    err, _r, v, (x, y, z), _jd = _propagate_raw(sats, times)
    lat, lon, alt = ecef_to_geodetic(x, y, z)
    speed = np.linalg.norm(v, axis=-1)
    ok = (err == 0) & np.isfinite(lat)
    return {"lat": lat, "lon": lon, "alt_km": alt, "speed_kms": speed, "ok": ok}


# -- view from a place on the ground -------------------------------------------
_R_EARTH_SHADOW = 6371.0  # km, mean radius used for the (cylindrical) shadow test


def geodetic_to_ecef(lat_deg: float, lon_deg: float, h_km: float = 0.0):
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    n = _A / np.sqrt(1.0 - _E2 * np.sin(lat) ** 2)
    return np.array([
        (n + h_km) * np.cos(lat) * np.cos(lon),
        (n + h_km) * np.cos(lat) * np.sin(lon),
        (n * (1.0 - _E2) + h_km) * np.sin(lat),
    ])


def sun_direction(jd: np.ndarray) -> np.ndarray:
    """Unit vector to the Sun in the equatorial (TEME-like) frame, shape (n_times, 3).

    Low-precision solar formula (~0.01 deg), plenty for an Earth-shadow test.
    """
    d = jd - 2451545.0
    g = np.radians(357.529 + 0.98560028 * d)
    q = 280.459 + 0.98564736 * d
    lam = np.radians(q + 1.915 * np.sin(g) + 0.020 * np.sin(2 * g))
    eps = np.radians(23.439 - 0.00000036 * d)
    return np.stack([np.cos(lam), np.cos(eps) * np.sin(lam), np.sin(eps) * np.sin(lam)], axis=-1)


def look_angles(sats: list[Satellite], times: list[datetime], lat_deg: float, lon_deg: float,
                h_km: float = 0.0):
    """Azimuth/elevation of satellites seen from a place on the ground.

    Returns dict of arrays shaped (n_sats, n_times):
      az (deg, 0 = North, 90 = East), el (deg above the horizon), range_km,
      sunlit (True if the satellite is in sunlight, i.e. outside Earth's shadow),
      ok (SGP4 succeeded).
    """
    err, r_teme, _v, (x, y, z), jd = _propagate_raw(sats, times)
    obs = geodetic_to_ecef(lat_deg, lon_deg, h_km)
    dx, dy, dz = x - obs[0], y - obs[1], z - obs[2]

    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    sl, cl, so, co = np.sin(lat), np.cos(lat), np.sin(lon), np.cos(lon)
    east = -so * dx + co * dy
    north = -sl * co * dx - sl * so * dy + cl * dz
    up = cl * co * dx + cl * so * dy + sl * dz

    az = np.mod(np.degrees(np.arctan2(east, north)), 360.0)
    el = np.degrees(np.arctan2(up, np.hypot(east, north)))
    rng = np.sqrt(dx * dx + dy * dy + dz * dz)

    # Cylindrical Earth shadow: in shadow if behind Earth (relative to the Sun)
    # and closer to the Earth-Sun axis than one Earth radius.
    sun = sun_direction(jd)                       # (n_times, 3)
    along = np.einsum("stk,tk->st", r_teme, sun)  # (n_sats, n_times)
    perp = np.sqrt(np.maximum(np.einsum("stk,stk->st", r_teme, r_teme) - along ** 2, 0.0))
    sunlit = (along > 0) | (perp > _R_EARTH_SHADOW)

    ok = (err == 0) & np.isfinite(el)
    return {"az": az, "el": el, "range_km": rng, "sunlit": sunlit, "ok": ok}
