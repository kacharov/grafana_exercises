# Real-time satellite tracker in Grafana

Python service that downloads orbital elements from **Space-Track.org**, propagates
them with **SGP4** to the current time, and serves latitude/longitude/altitude as JSON.
Grafana reads it through the **Infinity** data source and draws it on an animated world map
(**Business Charts** / Apache ECharts panel). The map replays the satellites' motion over the selected
time range, then stops at the end time. With "Last 1 hour" that means *now*, kept live by auto-refresh.

```
Space-Track.org ──(every 4 h, cached in ./data)──► satapi (FastAPI + sgp4, :8000)
                                                       │  JSON over HTTP
                                                       ▼
                                Grafana (:3000) ── Infinity data source ── animated map panel
```

Only element sets come from Space-Track. Positions are computed locally on every
refresh, so the map is real time without hammering Space-Track's API.

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
cp .env.example .env          # put your Space-Track username and password in .env
docker compose up -d --build
```

Open http://localhost:3000 and log in as `admin` / `admin` (or the password from `.env`).
The **Satellite tracker** dashboard is the home page.

* **Constellation**: Featured (ISS, Tiangong, Hubble), weather sats, Starlink, OneWeb, Kuiper,
  Iridium, GPS, Galileo, BeiDou, or your own list.
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

The first time you open a constellation, the API downloads it from Space-Track. This takes a few
seconds (Starlink is about 9,000 objects). After that it reads from the cache in `./data/`.

## Running without Docker (development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r satapi/requirements.txt
cd satapi && uvicorn app:app --reload --port 8000     # reads ../.env
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
"planet":   { "label": "Planet Flock", "filters": {"OBJECT_NAME": "^FLOCK", "OBJECT_TYPE": "PAYLOAD"} }
```

`filters` are Space-Track GP-class predicates: `^ABC` means "starts with", and `~~ABC` means "contains".
NORAD IDs can be looked up at https://www.space-track.org or https://celestrak.org/satcat/search.php.

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

## Space-Track usage rules (important)

* Rate limits: **≤ 30 requests/min and ≤ 300/hour**. Don't download the same GP data more than once
  per hour. The API caches to `./data/` and refreshes every `REFRESH_HOURS` (default 4, minimum 1).
* Space-Track's user agreement prohibits redistributing its data to third parties. Keep this
  Grafana instance private: don't publish the dashboard publicly or expose port 3000/8000 to the internet.
* Never commit `.env` (it's in `.gitignore`).

## Accuracy

SGP4 with fresh element sets is accurate to about 1 km, degrading by a few km per day of element-set
age (shown in the table and the "Data status" panel). That's far more than enough for a world map.

## Credits

Constellation lines and label positions (`satapi/sky_chart.json`) come from
[d3-celestial](https://github.com/ofrohn/d3-celestial) by Olaf Frohn, BSD-3-Clause licence
(`satapi/sky_chart.LICENSE`). To show more or fewer constellations, edit that file.
