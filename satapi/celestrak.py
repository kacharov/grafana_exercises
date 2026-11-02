"""CelesTrak client with an on-disk cache.

CelesTrak (https://celestrak.org) publishes the same general-perturbations
(GP) orbital elements as Space-Track, freely and without an account.

Its usage policy (https://celestrak.org/usage-policy.php) asks automated
clients to:
  * not download the same data more than about once per hour; the GP data
    itself only changes every 2 hours. For big groups (Starlink, active)
    this is enforced: an early repeat gets HTTP 403 "GP data has not updated
    since your last successful download ...";
  * stop querying immediately on any other non-200 response (301, 403, 404,
    50x) and let a human look. Ignoring this gets the IP address firewalled.

So this module:
  * caches every constellation on disk and refreshes it at most every
    REFRESH_HOURS (default 4 h, never less than 2 h);
  * treats the "has not updated" 403 as "keep the cached data and try again
    next cycle";
  * on any other error, pauses ALL CelesTrak requests for PAUSE_HOURS
    (default 6 h), keeps serving the cache, and reports the problem in /health
    and the API log;
  * downloads the CSV (OMM) format, which CelesTrak recommends now that
    5-digit catalog numbers have run out.

Positions are then computed locally with SGP4, which is what makes the map
"real time". Visitors of the dashboard never cause more downloads than this.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests

from propagate import Satellite, satellite_from_gp

log = logging.getLogger("celestrak")

GP_URL = os.getenv("CELESTRAK_GP_URL", "https://celestrak.org/NORAD/elements/gp.php")
USER_AGENT = "grafana-satellite-tracker/2.0 (+https://github.com/kacharov/grafana_exercises)"

# Debris and rocket bodies sometimes match name searches ("... DEB", "... R/B").
_NOT_A_SATELLITE = re.compile(r"\bDEB\b|R/B|\bAKM\b|\bPKM\b")


class CelesTrakError(RuntimeError):
    pass


class NotUpdated(Exception):
    """CelesTrak says the data hasn't changed since our last download (HTTP 403)."""


class CelesTrakClient:
    MIN_INTERVAL_S = 2.0  # spacing between requests

    def __init__(self, pause_hours: float = 6.0, timeout: float = 120):
        self.timeout = timeout
        self.pause_s = pause_hours * 3600
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._lock = threading.Lock()
        self._last_call = 0.0
        self.paused_until = 0.0
        self.last_error: str | None = None

    def _throttle(self):
        wait = self.MIN_INTERVAL_S - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _pause(self, reason: str):
        self.paused_until = time.time() + self.pause_s
        self.last_error = reason
        log.error("CelesTrak: %s -> pausing all downloads for %.0f h (cached data is still served). "
                  "If this keeps happening, check https://celestrak.org/usage-policy.php",
                  reason, self.pause_s / 3600)

    def status(self) -> dict:
        paused = time.time() < self.paused_until
        return {
            "paused": paused,
            "paused_until": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.paused_until)) if paused else None,
            "last_error": self.last_error,
        }

    def query(self, **params) -> list[dict]:
        """GET gp.php with the given parameters (GROUP=..., CATNR=..., NAME=...) as CSV.

        Returns a list of OMM records (dicts of strings). Raises NotUpdated when
        CelesTrak reports no change since our last download, CelesTrakError otherwise.
        """
        with self._lock:
            if time.time() < self.paused_until:
                raise CelesTrakError(
                    f"CelesTrak downloads are paused until {self.status()['paused_until']} "
                    f"after an error: {self.last_error}")
            self._throttle()
            params = {**params, "FORMAT": "csv"}
            log.info("GET %s %s", GP_URL, params)
            try:
                r = self.session.get(GP_URL, params=params, timeout=self.timeout, allow_redirects=False)
            except requests.RequestException as exc:
                # Network trouble on our side: not a policy issue, so no long pause.
                raise CelesTrakError(f"Could not reach CelesTrak: {exc}") from exc

            text = r.text.strip()
            if r.status_code == 403 and "has not updated" in text:
                log.info("CelesTrak: %s not updated since our last download (that's fine)", params)
                raise NotUpdated(text[:200])
            if r.status_code != 200:
                self._pause(f"HTTP {r.status_code} for {params}: {text[:200]}")
                raise CelesTrakError(self.last_error)
            if text.upper().startswith("NO GP DATA FOUND"):
                return []
            if not text.startswith("OBJECT_NAME"):
                self._pause(f"unexpected response for {params}: {text[:200]}")
                raise CelesTrakError(self.last_error)
            return list(csv.DictReader(io.StringIO(text)))


class ConstellationStore:
    """Holds parsed satellites per constellation, backed by a JSON cache on disk.

    Each entry of constellations.json says how to get a constellation:
      "norad_ids": [25544, ...]          one CATNR query per satellite
      "celestrak": {"GROUP": "starlink"} a CelesTrak group
      "celestrak": {"NAME": "FLOCK"}     a name search (contains)
    plus optionally "name_prefix": "FLOCK" to keep only names starting with it.
    Name searches automatically drop debris and rocket bodies.
    """

    def __init__(self, client: CelesTrakClient, config_path: Path, cache_dir: Path,
                 refresh_hours: float = 4.0):
        self.client = client
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.refresh_s = max(2.0, refresh_hours) * 3600  # CelesTrak updates every 2 h
        self.groups: dict[str, dict] = json.loads(Path(config_path).read_text())
        self._mem: dict[str, dict] = {}  # group -> {"fetched_at", "records", "sats"}
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
            # Nothing cached at all: we have to wait for CelesTrak once.
            with self._locks[group]:
                entry = self._mem.get(group) or self._fetch(group)
        elif time.time() - entry["fetched_at"] > self.refresh_s:
            self._refresh_in_background(group)
        return entry["sats"]

    def fetched_at(self, group: str) -> float | None:
        entry = self._mem.get(group)
        return entry["fetched_at"] if entry else None

    def status(self) -> dict:
        groups = {}
        for g in self.groups:
            entry = self._mem.get(g)
            if entry is None and self._cache_file(g).exists():
                entry = self._load_disk(g)
            groups[g] = ({"satellites": len(entry["sats"]),
                          "cache_age_min": round((time.time() - entry["fetched_at"]) / 60, 1)}
                         if entry else {"satellites": None, "cache_age_min": None})
        return {"source": "CelesTrak", "downloads": self.client.status(), "groups": groups}

    # -- internals ----------------------------------------------------------
    def _cache_file(self, group: str) -> Path:
        return self.cache_dir / f"celestrak_{group}.json"

    def _load_disk(self, group: str):
        path = self._cache_file(group)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
            entry = {"fetched_at": payload["fetched_at"], "records": payload["records"],
                     "sats": self._parse(payload["records"])}
        except Exception as exc:  # corrupt cache -> refetch
            log.warning("Ignoring unreadable cache %s: %s", path, exc)
            return None
        self._mem[group] = entry
        return entry

    def _download(self, group: str, old_records: list[dict] | None) -> list[dict]:
        cfg = self.groups[group]
        if cfg.get("norad_ids"):
            old_by_id = {r.get("NORAD_CAT_ID"): r for r in (old_records or [])}
            records = []
            for nid in cfg["norad_ids"]:
                try:
                    records += self.client.query(CATNR=int(nid))
                except NotUpdated:
                    if str(nid) in old_by_id:
                        records.append(old_by_id[str(nid)])
            return records

        query = cfg.get("celestrak")
        if not query:
            raise CelesTrakError(f"Constellation '{group}' needs 'norad_ids' or 'celestrak' in constellations.json")
        try:
            records = self.client.query(**query)
        except NotUpdated:
            if old_records is None:
                raise CelesTrakError(
                    f"CelesTrak says '{group}' hasn't changed since the last download, but there is no "
                    "cached copy. It will be available after CelesTrak's next 2-hourly update.")
            return old_records
        if "NAME" in query:
            records = [r for r in records if not _NOT_A_SATELLITE.search(r.get("OBJECT_NAME", ""))]
        prefix = cfg.get("name_prefix")
        if prefix:
            records = [r for r in records if r.get("OBJECT_NAME", "").upper().startswith(prefix.upper())]
        return records

    def _fetch(self, group: str):
        old = self._mem.get(group) or self._load_disk(group)
        records = self._download(group, old["records"] if old else None)
        now = time.time()
        tmp = self._cache_file(group).with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": now, "records": records}))
        tmp.replace(self._cache_file(group))
        entry = {"fetched_at": now, "records": records, "sats": self._parse(records)}
        self._mem[group] = entry
        log.info("Group %s: %d satellites loaded from CelesTrak", group, len(entry["sats"]))
        return entry

    def _refresh_in_background(self, group: str):
        if group in self._refreshing or time.time() < self.client.paused_until:
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
    client = CelesTrakClient(pause_hours=float(os.getenv("PAUSE_HOURS", "6")))
    return ConstellationStore(
        client,
        config_path=Path(os.getenv("CONSTELLATIONS_FILE", here / "constellations.json")),
        cache_dir=Path(os.getenv("CACHE_DIR", here.parent / "data")),
        refresh_hours=float(os.getenv("REFRESH_HOURS", "4")),
    )
