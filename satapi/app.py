"""Satellite position API for Grafana (Infinity data source).

Endpoints (all return flat JSON arrays, which Infinity turns into tables):
  GET /constellations                       -> dropdown values for $constellation
  GET /satellites?group=starlink            -> dropdown values for $satellite
  GET /positions?group=..&ids=..&t=..       -> current sub-satellite points
  GET /tracks?group=..&ids=..&minutes=..    -> ground-track points around now
  GET /animation?group=..&ids=..&from=..&to=..  -> positions at time steps for the replay
  GET /summary?group=..&ids=..              -> counts and data freshness
  Sky view (what an observer on the ground sees):
  GET /cities                               -> dropdown values for $city
  GET /observer?city=..&coords=..           -> name, lat, lon of the observer
  GET /sky?group=..&ids=..&from=..&to=..&city=..&coords=..&min_el=..
                                            -> azimuth/elevation of satellites above the horizon
  GET /visible?group=..&ids=..&t=..&city=..&coords=..
                                            -> satellites above the horizon at one moment
  GET /sky_chart                            -> constellation stick figures (RA/Dec)
  GET /health

`ids` is a comma-separated list of NORAD catalogue numbers, or `all`.
`t` is an optional time in epoch milliseconds (Grafana's ${__to}); default now.
"""
from __future__ import annotations

import logging
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query

import json
from pathlib import Path

import numpy as np

from propagate import Satellite, look_angles, propagate
from spacetrack import SpaceTrackError, store_from_env

try:  # load .env when running outside Docker
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Satellite tracker API", version="1.0")
store = store_from_env()

MAX_TRACK_SATS = int(os.getenv("MAX_TRACK_SATS", "25"))


# -- helpers -----------------------------------------------------------------
def _when(t: float | None) -> datetime:
    if t is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)


def _select(group: str, ids: str) -> list[Satellite]:
    try:
        sats = store.get(group)
    except KeyError:
        raise HTTPException(404, f"Unknown constellation '{group}'. See /constellations.")
    except SpaceTrackError as exc:
        raise HTTPException(503, str(exc))
    ids = (ids or "").strip()
    if ids.lower() in ("", "all", "$__all", "*"):
        return sats
    wanted = set()
    for part in ids.replace("{", "").replace("}", "").split(","):
        part = part.strip()
        if part.isdigit():
            wanted.add(int(part))
    return [s for s in sats if s.norad_id in wanted]


# -- endpoints ---------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "groups": [g for g, _ in store.list_groups()]}


@app.get("/constellations")
def constellations():
    return [{"__value": key, "__text": label} for key, label in store.list_groups()]


@app.get("/satellites")
def satellites(group: str = Query("featured")):
    sats = sorted(_select(group, "all"), key=lambda s: s.name)
    return [{"__value": str(s.norad_id), "__text": f"{s.name} [{s.norad_id}]"} for s in sats]


@app.get("/positions")
def positions(
    group: str = Query("featured"),
    ids: str = Query("all"),
    t: float | None = Query(None, description="epoch ms; default now"),
    label_limit: int = Query(-1, description="if >=0 and more satellites than this are selected, return [] (used for map labels)"),
):
    sats = _select(group, ids)
    if label_limit >= 0 and len(sats) > label_limit:
        return []
    when = _when(t)
    res = propagate(sats, [when])
    out = []
    for i, s in enumerate(sats):
        if not res["ok"][i, 0]:
            continue
        out.append({
            "name": s.name,
            "norad_id": s.norad_id,
            "lat": round(float(res["lat"][i, 0]), 4),
            "lon": round(float(res["lon"][i, 0]), 4),
            "alt_km": round(float(res["alt_km"][i, 0]), 1),
            "speed_kms": round(float(res["speed_kms"][i, 0]), 3),
            "tle_age_h": round((when - s.epoch).total_seconds() / 3600.0, 1),
        })
    return out


@app.get("/tracks")
def tracks(
    group: str = Query("featured"),
    ids: str = Query("all"),
    t: float | None = Query(None),
    minutes: float = Query(90, ge=0, le=1440, description="minutes ahead of t"),
    past_minutes: float = Query(30, ge=0, le=1440, description="minutes before t"),
    step_s: float = Query(60, ge=10, le=600),
):
    sats = _select(group, ids)
    if minutes == 0 and past_minutes == 0:
        return []
    if len(sats) > MAX_TRACK_SATS:
        # Ground tracks for hundreds of satellites are unreadable (and heavy).
        return []
    when = _when(t)
    n_steps = int((minutes + past_minutes) * 60 // step_s) + 1
    offsets = [-past_minutes * 60 + k * step_s for k in range(n_steps)]
    times = [when + timedelta(seconds=o) for o in offsets]
    res = propagate(sats, times)
    out = []
    for i, s in enumerate(sats):
        for j, off in enumerate(offsets):
            if not res["ok"][i, j]:
                continue
            out.append({
                "name": s.name,
                "norad_id": s.norad_id,
                "lat": round(float(res["lat"][i, j]), 3),
                "lon": round(float(res["lon"][i, j]), 3),
                "alt_km": round(float(res["alt_km"][i, j]), 1),
                "minutes_from_now": round(off / 60.0, 1),
            })
    return out


MAX_ANIMATION_HOURS = float(os.getenv("MAX_ANIMATION_HOURS", "24"))
ANIMATION_POINT_BUDGET = int(os.getenv("ANIMATION_POINT_BUDGET", "60000"))  # soft target
ANIMATION_MAX_ROWS = int(os.getenv("ANIMATION_MAX_ROWS", "150000"))  # hard limit (~15 MB of JSON)
ANIMATION_MAX_STEP_S = 600.0  # LEO satellites move ~40 deg in 10 min; longer steps can't be interpolated


@app.get("/animation")
def animation(
    group: str = Query("featured"),
    ids: str = Query("all"),
    start: float = Query(..., alias="from", description="epoch ms (Grafana ${__from})"),
    end: float = Query(..., alias="to", description="epoch ms (Grafana ${__to})"),
):
    """Positions of the selected satellites at evenly spaced times from `from` to `to`.

    One row per satellite per time step, consumed by the animated map panel.
    The step is at least 30 s and grows so that satellites x steps stays near
    ANIMATION_POINT_BUDGET, but never beyond 10 min (the ISS over 1 h: 121 steps;
    all of Starlink over 1 h: 7 steps). The panel interpolates smoothly between
    steps. Requests above ANIMATION_MAX_ROWS (e.g. all of Starlink over 3 h) are
    refused with a message asking for fewer satellites or a shorter range.
    """
    duration_s = (end - start) / 1000.0
    if duration_s <= 0:
        raise HTTPException(400, "The time range must end after it starts.")
    if duration_s > MAX_ANIMATION_HOURS * 3600:
        raise HTTPException(
            400,
            f"Time range too long for the animation (max {MAX_ANIMATION_HOURS:g} h). "
            "Pick a shorter range, e.g. 'Last 1 hour'.",
        )
    sats = _select(group, ids)
    if not sats:
        return []

    step_s = max(30.0, duration_s / 600.0, duration_s * len(sats) / ANIMATION_POINT_BUDGET)
    step_s = min(step_s, ANIMATION_MAX_STEP_S)
    n_steps = int(-(-duration_s // step_s)) + 1  # ceil, so both ends are included
    if n_steps * len(sats) > ANIMATION_MAX_ROWS:
        max_h = ANIMATION_MAX_ROWS / len(sats) * ANIMATION_MAX_STEP_S / 3600
        raise HTTPException(
            400,
            f"{len(sats)} satellites are too many to animate over this range. Select fewer "
            f"satellites or a range of at most {max(max_h, 0.1):.1f} h.",
        )
    t0 = _when(start)
    offsets = [duration_s * k / (n_steps - 1) for k in range(n_steps)]
    times = [t0 + timedelta(seconds=o) for o in offsets]
    stamps = [int(start + o * 1000) for o in offsets]

    res = propagate(sats, times)
    out = []
    for i, s in enumerate(sats):
        if not res["ok"][i].all():  # skip satellites SGP4 can't follow over the whole range
            continue
        lat, lon, alt = res["lat"][i], res["lon"][i], res["alt_km"][i]
        for j in range(n_steps):
            out.append({
                "norad_id": s.norad_id,
                "name": s.name if j == 0 else "",  # name sent once per satellite, saves bandwidth
                "t": stamps[j],
                "lat": round(float(lat[j]), 3),
                "lon": round(float(lon[j]), 3),
                "alt_km": round(float(alt[j])),
            })
    return out


# -- sky view ----------------------------------------------------------------
HERE = Path(__file__).parent
CITIES: dict = json.loads((HERE / "cities.json").read_text())
SKY_CHART: dict = json.loads((HERE / "sky_chart.json").read_text())
SKY_MAX_PROPAGATIONS = int(os.getenv("SKY_MAX_PROPAGATIONS", "3000000"))  # satellites x time steps
SKY_CHUNK = 1500  # satellites per SGP4 batch (keeps memory use small)


def _observer(city: str, coords: str) -> tuple[str, float, float]:
    """Resolve the observer from a city key, or from 'lat, lon' text when city == 'custom'."""
    city = (city or "").strip().lower()
    if city and city != "custom":
        if city not in CITIES:
            raise HTTPException(404, f"Unknown city '{city}'. See /cities.")
        c = CITIES[city]
        return c["label"], float(c["lat"]), float(c["lon"])
    parts = (coords or "").replace(",", " ").replace(";", " ").split()
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except (IndexError, ValueError):
        raise HTTPException(400, "Custom coordinates must look like '52.52, 13.405' (latitude, longitude in degrees).")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Latitude must be within -90..90 and longitude within -180..180.")
    ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
    return f"{abs(lat):.3f}°{ns}, {abs(lon):.3f}°{ew}", lat, lon


def _look_angles_chunked(sats, times, lat, lon):
    """look_angles() in batches of satellites; yields (offset, result dict)."""
    for i in range(0, len(sats), SKY_CHUNK):
        yield i, look_angles(sats[i:i + SKY_CHUNK], times, lat, lon)


@app.get("/cities")
def cities():
    out = [{"__value": k, "__text": v["label"]} for k, v in CITIES.items()]
    out.append({"__value": "custom", "__text": "Custom coordinates (enter below)"})
    return out


@app.get("/observer")
def observer(city: str = Query("berlin"), coords: str = Query("")):
    name, lat, lon = _observer(city, coords)
    return [{"name": name, "lat": lat, "lon": lon}]


@app.get("/sky_chart")
def sky_chart():
    """Constellation lines (kind=line, one row per vertex) and labels (kind=label)."""
    out, line_no = [], 0
    for c in SKY_CHART["constellations"]:
        for line in c["lines"]:
            for ra, dec in line:
                out.append({"kind": "line", "con": c["id"], "line": line_no, "ra": ra, "dec": dec, "name": ""})
            line_no += 1
        out.append({"kind": "label", "con": c["id"], "line": -1,
                    "ra": c["label"][0], "dec": c["label"][1], "name": c["name"]})
    return out


@app.get("/sky")
def sky(
    group: str = Query("featured"),
    ids: str = Query("all"),
    start: float = Query(..., alias="from", description="epoch ms (Grafana ${__from})"),
    end: float = Query(..., alias="to", description="epoch ms (Grafana ${__to})"),
    city: str = Query("berlin"),
    coords: str = Query(""),
    min_el: float = Query(0, ge=-5, le=89),
):
    """Azimuth/elevation of satellites above `min_el`, at evenly spaced times.

    Rows (only while a satellite is above min_el, plus one step either side so
    the panel can interpolate rising and setting): norad_id, name, k (step
    index), t (epoch ms), az, el, range_km, sunlit (1/0).
    Steps are 20 s (longer for ranges over 5 h) because a low-orbit pass only
    lasts a few minutes.
    """
    duration_s = (end - start) / 1000.0
    if duration_s <= 0:
        raise HTTPException(400, "The time range must end after it starts.")
    if duration_s > MAX_ANIMATION_HOURS * 3600:
        raise HTTPException(400, f"Time range too long for the sky view (max {MAX_ANIMATION_HOURS:g} h). "
                                 "Pick a shorter range, e.g. 'Last 1 hour'.")
    _, lat, lon = _observer(city, coords)
    sats = _select(group, ids)
    if not sats:
        return []

    step_s = max(20.0, duration_s / 900.0)
    n_steps = int(-(-duration_s // step_s)) + 1
    if n_steps * len(sats) > SKY_MAX_PROPAGATIONS:
        max_h = SKY_MAX_PROPAGATIONS / len(sats) * 20.0 / 3600
        raise HTTPException(400, f"{len(sats)} satellites are too many for the sky view over this range. "
                                 f"Select fewer satellites or a range of at most {max(max_h, 0.1):.1f} h.")
    offsets = [duration_s * k / (n_steps - 1) for k in range(n_steps)]
    t0 = _when(start)
    times = [t0 + timedelta(seconds=o) for o in offsets]
    stamps = [int(start + o * 1000) for o in offsets]

    out = []
    for off, res in _look_angles_chunked(sats, times, lat, lon):
        above = (res["el"] > min_el) & res["ok"]
        # keep one step before rising and after setting, for smooth interpolation
        keep = above.copy()
        keep[:, 1:] |= above[:, :-1]
        keep[:, :-1] |= above[:, 1:]
        keep &= res["ok"]
        for i in np.nonzero(keep.any(axis=1))[0]:
            s = sats[off + i]
            first = True
            for k in np.nonzero(keep[i])[0]:
                out.append({
                    "norad_id": s.norad_id,
                    "name": s.name if first else "",
                    "k": int(k),
                    "t": stamps[k],
                    "az": round(float(res["az"][i, k]), 2),
                    "el": round(float(res["el"][i, k]), 2),
                    "range_km": round(float(res["range_km"][i, k])),
                    "sunlit": int(res["sunlit"][i, k]),
                })
                first = False
    if len(out) > ANIMATION_MAX_ROWS:
        raise HTTPException(400, "Too many satellite passes for this range. Select fewer satellites or a shorter range.")
    return out


@app.get("/visible")
def visible(
    group: str = Query("featured"),
    ids: str = Query("all"),
    t: float | None = Query(None),
    city: str = Query("berlin"),
    coords: str = Query(""),
    min_el: float = Query(0, ge=-5, le=89),
):
    """Satellites above `min_el` at one moment, highest first."""
    _, lat, lon = _observer(city, coords)
    sats = _select(group, ids)
    out = []
    for off, res in _look_angles_chunked(sats, [_when(t)], lat, lon):
        for i in np.nonzero((res["el"][:, 0] > min_el) & res["ok"][:, 0])[0]:
            s = sats[off + i]
            out.append({
                "name": s.name,
                "norad_id": s.norad_id,
                "az": round(float(res["az"][i, 0]), 1),
                "el": round(float(res["el"][i, 0]), 1),
                "range_km": round(float(res["range_km"][i, 0])),
                "sunlit": "yes" if res["sunlit"][i, 0] else "no (Earth's shadow)",
            })
    out.sort(key=lambda r: -r["el"])
    return out


@app.get("/summary")
def summary(group: str = Query("featured"), ids: str = Query("all"), t: float | None = Query(None)):
    sats = _select(group, ids)
    when = _when(t)
    ages = [(when - s.epoch).total_seconds() / 3600.0 for s in sats]
    fetched = store.fetched_at(group)
    return [{
        "satellites": len(sats),
        "median_tle_age_h": round(statistics.median(ages), 1) if ages else None,
        "data_fetched_min_ago": round((time.time() - fetched) / 60.0, 1) if fetched else None,
    }]
