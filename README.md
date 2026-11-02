# Real-time satellite tracker in Grafana

Python service that downloads orbital elements from **[CelesTrak](https://celestrak.org)** (free,
no account needed), propagates them with **SGP4** to the current time, and serves latitude/longitude/altitude as JSON.
Grafana reads it through the **Infinity** data source and draws it on an animated world map
(**Business Charts** / Apache ECharts panel). The map replays the satellites' motion over the selected
time range, then stops at the end time. With "Last 1 hour" that means *now*, kept live by auto-refresh.

```
celestrak.org ──(every 4 h, cached in ./data)──► satapi (FastAPI + sgp4, :8000)
                                                       │  JSON over HTTP
                                                       ▼
                                Grafana (:3000) ── Infinity data source ── animated map panel
```

Only element sets come from CelesTrak. Positions are computed locally on every
refresh, so the map is real time without hammering CelesTrak. Dashboard visitors never cause extra
downloads (see "CelesTrak usage rules" below).

## What to install

| Tool | Why | How |
|---|---|---|
| **Docker Desktop** (recommended) | Runs Grafana and the API with one command | https://www.docker.com/products/docker-desktop |
| **Grafana OSS** (v12 or newer; compose uses `latest`) | Dashboards | Pulled automatically by `docker compose` |
| **Infinity data source plugin** (`yesoreyeram-infinity-datasource`) | Lets Grafana query any JSON/REST API | Installed automatically through `GF_PLUGINS_PREINSTALL_SYNC` |
| **Business Charts panel plugin** (`volkovlabs-echarts-panel`) | Animated map (Grafana's built-in Geomap can't animate) | Installed automatically through `GF_PLUGINS_PREINSTALL_SYNC` |
| Python 3.10+ | Runs the API (inside Docker, or locally for development) | `brew install python` |

You don't need anything else: no database and no map service (the world map ships with the plugin).

## Quick start (Docker)

```bash
cd /Users/kacharov/python/claude/grafana_exercises
cp .env.example .env          # optional: set a Grafana admin password (no credentials needed for data)
docker compose up -d --build
```

Open http://localhost:3000 and log in as `admin` / `admin` (or the password from `.env`).
The **Satellite tracker** dashboard is the home page.

* **Constellation**: Featured (ISS, Tiangong, Hubble), weather sats, Starlink, OneWeb, Kuiper,
  Iridium NEXT, GPS, Galileo, BeiDou, Planet SuperDove and Pelican, or your own list.
* **Satellite**: one, several, or All within the constellation.
* **Time range** (time picker, max 24 h): the map replays the motion from the start to the end of
  the range, then stops at the end. "Last 1 hour" replays the past hour and stops at *now*. A range
  like "now-30m to now+1h" plays into the future. ▶ (top right of the map) replays.
* **Replay length**: how many seconds the replay takes (5–30 s).
* **Label if ≤ N sats**: name labels on the map appear only when few satellites are shown. Paths
  travelled are drawn when 25 or fewer satellites are shown.
* Auto-refresh (every minute) doesn't replay. It moves the satellites to the new *now*.
* **Sky view** (below the map): the sky seen from a place on the ground. Pick a **City**, or choose
  "Custom coordinates" and type them in **Custom lat, lon** (e.g. `42.70, 23.32`). Zenith is in the centre and
  the horizon on the outer circle, with North up and East on the left, as on a star chart held overhead.
  Satellites above the horizon move across it over the time range, like the map:
  * yellow = sunlit (visible to the eye when your sky is dark), grey = in Earth's shadow
  * faint blue grid = celestial parallels and meridians; faint lines = 26 main constellations
  * the background brightens with the Sun's altitude, and the Sun is drawn when it's up
  **Min. elevation** hides satellites lower than e.g. 10° (usually behind buildings and trees). The table next
  to it lists what is above the horizon at the end time.
* Large constellations over long ranges are refused with a message (e.g. all ~11,000 Starlink
  satellites work up to about 2 h). Select fewer satellites or a shorter range.

The first time you open a constellation, the API downloads it from CelesTrak. This takes a few
seconds (Starlink is about 11,000 objects). After that it reads from the cache in `./data/`
(`celestrak_<group>.json`). Old `gp_<group>.json` files from Space-Track are no longer used and can be deleted.

## Running without Docker (development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r satapi/requirements.txt
cd satapi && uvicorn app:app --reload --port 8000     # reads ../.env if present
# test: http://localhost:8000/docs  or
curl "http://localhost:8000/positions?group=featured&ids=all"
```

To run Grafana natively instead (macOS):

```bash
brew install grafana
grafana cli --homepath "$(brew --prefix)/opt/grafana/share/grafana" plugins install yesoreyeram-infinity-datasource
grafana cli --homepath "$(brew --prefix)/opt/grafana/share/grafana" plugins install volkovlabs-echarts-panel
# provisioning: copy the datasource + dashboard config
cp grafana/provisioning/datasources/satapi.yml "$(brew --prefix)/etc/grafana/provisioning/datasources/"
sed "s#/etc/grafana/dashboards#$PWD/grafana/dashboards#" grafana/provisioning/dashboards/satellites.yml \
  > "$(brew --prefix)/etc/grafana/provisioning/dashboards/satellites.yml"
SATAPI_URL=http://localhost:8000 brew services restart grafana
```

(`brew services` may not pass `SATAPI_URL` through. If the data source shows an empty URL, set it
to `http://localhost:8000` in *Connections → Data sources → Satellite API*.)

## Adding your own satellites or constellations

Edit `satapi/constellations.json`, then restart the API (`docker compose restart satapi`).

```json
"custom":   { "label": "My satellites", "norad_ids": [25544, 43013] },
"gnss":     { "label": "All GNSS", "celestrak": {"GROUP": "gnss"} },
"planet":   { "label": "Planet Flock", "celestrak": {"NAME": "FLOCK"}, "name_prefix": "FLOCK" }
```

Three ways to define a constellation:

* `norad_ids`: individual satellites by catalog number. Each one is a separate CelesTrak request, so keep
  these lists short (up to ~20).
* `celestrak` with `GROUP`: one of CelesTrak's groups, e.g. `stations`, `starlink`, `oneweb`,
  `iridium-NEXT`, `gps-ops`, `galileo`, `gnss`, `weather`, `planet`, `visual`. Listed at
  https://celestrak.org/NORAD/elements/.
* `celestrak` with `NAME`: all satellites whose name *contains* the text. Debris and rocket bodies are
  dropped automatically. Add `name_prefix` to keep only names that *start* with a text.

Catalog numbers can be looked up at https://celestrak.org/satcat/search.php.

## API endpoints

| Endpoint | Returns |
|---|---|
| `/constellations` | `__text`/`__value` pairs for the dropdown |
| `/satellites?group=starlink` | satellites in a group |
| `/positions?group=..&ids=all\|25544,20580&t=<epoch ms>` | name, norad_id, lat, lon, alt_km, speed_kms, tle_age_h |
| `/tracks?group=..&ids=..&minutes=90&past_minutes=30&step_s=60` | ground-track points |
| `/animation?group=..&ids=..&from=<ms>&to=<ms>` | positions at evenly spaced times for the replay (max 24 h) |
| `/sky?group=..&ids=..&from=..&to=..&city=berlin\|custom&coords=lat,lon&min_el=0` | azimuth/elevation/range/sunlit while above the horizon (sky view) |
| `/visible?group=..&ids=..&t=..&city=..&coords=..` | satellites above the horizon at one moment |
| `/cities`, `/observer?city=..&coords=..` | city dropdown, resolved observer position |
| `/sky_chart` | constellation stick figures (RA/Dec) |
| `/summary?group=..&ids=..` | count, median element age, cache age |
| `/health` | CelesTrak download status (paused? last error) and cache age per constellation |

## CelesTrak usage rules

CelesTrak is a free service run by one person (Dr T.S. Kelso). Its
[usage policy](https://celestrak.org/usage-policy.php) asks automated software to download the same data
at most about once per hour (the data only changes every 2 hours), and to **stop immediately on any error
response**; software that keeps hammering gets its IP address firewalled. The API follows these rules:

* Each constellation is cached in `./data/` and re-downloaded at most every `REFRESH_HOURS`
  (default 4, minimum 2). Dashboard viewers only ever read the cache, however many there are.
* CelesTrak's HTTP 403 "GP data has not updated since your last successful download" is expected now
  and then: the cached data is kept and the API waits for the next cycle.
* Any other error (e.g. 404, 50x) **pauses all CelesTrak downloads for `PAUSE_HOURS`** (default 6).
  Cached constellations keep working; a constellation that was never downloaded shows the error
  message in its panels. `http://localhost:8000/health` shows whether downloads are paused and why.
  Restarting the API (`docker compose restart satapi`) clears the pause, so only do that once you know
  the cause has been fixed.
* Requests are spaced at least 2 s apart and send a `User-Agent` naming this project.

CelesTrak publishes this data openly, so a public dashboard is fine. Please keep the credit to
CelesTrak visible (it's in the dashboard description) and keep the refresh limits as they are.

## Accuracy

SGP4 with fresh element sets is accurate to about 1 km, degrading by a few km per day of element-set
age (shown in the table and the "Data status" panel). That's far more than enough for a world map.

## Credits

Orbital elements: [CelesTrak](https://celestrak.org), Dr T.S. Kelso.

Constellation lines and label positions (`satapi/sky_chart.json`) come from
[d3-celestial](https://github.com/ofrohn/d3-celestial) by Olaf Frohn, BSD-3-Clause licence
(`satapi/sky_chart.LICENSE`). To show more or fewer constellations, edit that file.
