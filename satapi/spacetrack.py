"""Space-Track.org client with an on-disk cache.

Space-Track asks API users to be gentle:
  * at most 30 requests per minute and 300 per hour,
  * do not re-download the same GP data more than about once per hour.
Element sets for most satellites are updated a few times a day at most,
so we cache every constellation on disk and refresh it every
REFRESH_HOURS (default 4h, never less than 1h). Positions are then computed
locally with SGP4, which is what makes the map "real time".
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import requests

from propagate import Satellite, satellite_from_gp

log = logging.getLogger("spacetrack")

BASE_URL = "https://www.space-track.org"
LOGIN_URL = f"{BASE_URL}/ajaxauth/login"
QUERY_URL = f"{BASE_URL}/basicspacedata/query/class/gp"

# Common predicates: still in orbit, and element set not older than 30 days.
_COMMON = "DECAY_DATE/null-val/EPOCH/>now-30/orderby/NORAD_CAT_ID/format/json"


class SpaceTrackError(RuntimeError):
    pass


class SpaceTrackClient:
    MIN_INTERVAL_S = 2.5  # keeps us well under 30 requests / minute

    def __init__(self, user: str, password: str, timeout: float = 120):
        self.user, self.password, self.timeout = user, password, timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "grafana-satellite-tracker/1.0"
        self._logged_in = False
        self._lock = threading.Lock()
        self._last_call = 0.0

    def _throttle(self):
        wait = self.MIN_INTERVAL_S - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _login(self):
        if not self.user or not self.password:
            raise SpaceTrackError(
                "SPACETRACK_USER / SPACETRACK_PASSWORD are not set (see .env.example)"
            )
        self._throttle()
        r = self.session.post(
            LOGIN_URL,
            data={"identity": self.user, "password": self.password},
            timeout=self.timeout,
        )
        if r.status_code != 200 or "Failed" in r.text:
            raise SpaceTrackError(f"Space-Track login failed ({r.status_code}): {r.text[:200]}")
        self._logged_in = True
        log.info("Logged in to Space-Track as %s", self.user)

    def query_gp(self, filters: dict | None = None, norad_ids: list[int] | None = None) -> list[dict]:
        """Return current GP (OMM) records matching filters or a list of NORAD IDs."""
        parts: list[str] = []
        if norad_ids:
            parts += ["NORAD_CAT_ID", ",".join(str(int(i)) for i in norad_ids)]
        for key, value in (filters or {}).items():
            parts += [key, str(value)]
        url = f"{QUERY_URL}/{'/'.join(parts)}/{_COMMON}"

        with self._lock:
            for attempt in range(2):
                if not self._logged_in:
                    self._login()
                self._throttle()
                log.info("GET %s", url)
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code == 401 and attempt == 0:
                    self._logged_in = False  # session expired -> log in again
                    continue
                if r.status_code != 200:
                    raise SpaceTrackError(f"Space-Track query failed ({r.status_code}): {r.text[:200]}")
                data = r.json()
                if isinstance(data, dict) and data.get("error"):
                    raise SpaceTrackError(str(data["error"]))
                return data
        raise SpaceTrackError("Space-Track query failed after re-login")


class ConstellationStore:
    """Holds parsed satellites per constellation, backed by a JSON cache on disk."""

    def __init__(self, client: SpaceTrackClient, config_path: Path, cache_dir: Path,
                 refresh_hours: float = 4.0):
        self.client = client
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.refresh_s = max(1.0, refresh_hours) * 3600
        self.groups: dict[str, dict] = json.loads(Path(config_path).read_text())
        self._mem: dict[str, dict] = {}  # group -> {"fetched_at": float, "sats": [...]}
        self._locks = {g: threading.Lock() for g in self.groups}
        self._refreshing: set[str] = set()

    # -- public -------------------------------------------------------------
    def list_groups(self) -> list[tuple[str, str]]:
        return [(key, cfg.get("label", key)) for key, cfg in self.groups.items()]

    def get(self, group: str) -> list[Satellite]:
        if group not in self.groups:
            raise KeyError(group)
        entry = self._mem.get(group) or self._load_disk(group)
        if entry is None:
            # Nothing cached at all: we have to wait for Space-Track once.
            with self._locks[group]:
                entry = self._mem.get(group) or self._fetch(group)
        elif time.time() - entry["fetched_at"] > self.refresh_s:
            self._refresh_in_background(group)
        return entry["sats"]

    def fetched_at(self, group: str) -> float | None:
        entry = self._mem.get(group)
        return entry["fetched_at"] if entry else None

    # -- internals ----------------------------------------------------------
    def _cache_file(self, group: str) -> Path:
        return self.cache_dir / f"gp_{group}.json"

    def _load_disk(self, group: str):
        path = self._cache_file(group)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
            entry = {"fetched_at": payload["fetched_at"], "sats": self._parse(payload["records"])}
        except Exception as exc:  # corrupt cache -> refetch
            log.warning("Ignoring unreadable cache %s: %s", path, exc)
            return None
        self._mem[group] = entry
        return entry

    def _fetch(self, group: str):
        cfg = self.groups[group]
        records = self.client.query_gp(filters=cfg.get("filters"), norad_ids=cfg.get("norad_ids"))
        now = time.time()
        tmp = self._cache_file(group).with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": now, "records": records}))
        tmp.replace(self._cache_file(group))
        entry = {"fetched_at": now, "sats": self._parse(records)}
        self._mem[group] = entry
        log.info("Group %s: %d satellites loaded from Space-Track", group, len(entry["sats"]))
        return entry

    def _refresh_in_background(self, group: str):
        if group in self._refreshing:
            return
        self._refreshing.add(group)

        def run():
            try:
                with self._locks[group]:
                    self._fetch(group)
            except Exception as exc:
                log.error("Background refresh of %s failed: %s", group, exc)
            finally:
                self._refreshing.discard(group)

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _parse(records: list[dict]) -> list[Satellite]:
        sats, seen = [], set()
        for rec in records:
            try:
                sat = satellite_from_gp(rec)
            except Exception as exc:
                log.debug("Skipping record %s: %s", rec.get("NORAD_CAT_ID"), exc)
                continue
            if sat.norad_id not in seen:
                seen.add(sat.norad_id)
                sats.append(sat)
        return sats


def store_from_env() -> ConstellationStore:
    here = Path(__file__).parent
    client = SpaceTrackClient(os.getenv("SPACETRACK_USER", ""), os.getenv("SPACETRACK_PASSWORD", ""))
    return ConstellationStore(
        client,
        config_path=Path(os.getenv("CONSTELLATIONS_FILE", here / "constellations.json")),
        cache_dir=Path(os.getenv("CACHE_DIR", here.parent / "data")),
        refresh_hours=float(os.getenv("REFRESH_HOURS", "4")),
    )
