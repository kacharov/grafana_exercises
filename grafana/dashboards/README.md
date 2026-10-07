# satellites.json: how the dashboard file is built

JSON can't contain comments; Grafana would refuse to load the file. Explanations therefore live in
two places: the `description` fields inside the file (shown in Grafana as ⓘ tooltips on panels and
dropdowns) and this guide. Grafana loads only `.json` files from this folder, so this README is ignored.

## Top level

| Key | Meaning |
|---|---|
| `uid` | Fixed dashboard ID (`sat-tracker`), used in the URL: `/d/sat-tracker` |
| `title`, `tags`, `description` | Name, search tags, and the text in the dashboard's info tooltip |
| `refresh: "1m"` | Re-run all queries every minute. With a relative range like "Last 1 hour" the end moves to the new *now*, so the map stays live |
| `time: now-1h → now` | Time range. The map replays the motion over the whole range (max 24 h). The table and status panels use only the **end** (`${__to}`) |
| `timezone: "utc"` | Times are shown in UTC |
| `templating.list` | The dropdowns at the top (see "Variables") |
| `panels` | The three panels (see "Panels") |

## Variables (dropdowns)

Grafana replaces `${name}` in any query with the variable's current value.

| Variable | Type | Source / values |
|---|---|---|
| `constellation` | query | Infinity query to `/constellations`. The API returns `__text` (label shown) and `__value` (key sent back, e.g. `starlink`) |
| `satellite` | query, multi-select, "All" | `/satellites?group=${constellation}`. Reloads when the constellation changes. "All" sends the value `all` (`allValue`) |
| `anim_seconds` | custom | `5,10,20,30`: how many seconds the replay takes |
| `label_limit` | custom | `0,10,30,100`: show names only when this many satellites or fewer are on the map |

| `city` | query | `/cities`: observer for the sky view. The value is a key such as `berlin`, or `custom` |
| `coords` | textbox | `lat, lon` used when `city` = `custom`. Sent as `${coords:percentencode}` because it contains a comma and a space |
| `min_el` | custom | `0,10,20,30`: minimum elevation for the sky view and the "above the horizon" table |

`${satellite:csv}` turns a multi-selection into `25544,20580`, which is the format the API expects.

## Queries (same shape everywhere)

Each panel's `targets` entry is an Infinity query:

```json
{
  "refId": "A",                         // name the panel uses to refer to this query
  "datasource": {"uid": "satapi"},      // the provisioned "Satellite API" data source
  "type": "json", "source": "url",      // fetch JSON from a URL
  "parser": "backend",                  // parse on the Grafana server; columns detected automatically
  "format": "table",                    // each JSON object becomes a table row
  "url": "/positions?group=${constellation}&ids=${satellite:csv}&t=${__to}"
}
```

The `url` is relative. Infinity adds the data source's base URL (`http://satapi:8000` in Docker).
`${__to}` is the end of the time range in epoch milliseconds.

## Panels

Numbers are the panel `id`s in the JSON, listed in screen order.

### 1. Animated map: "Satellite motion" (top, full width)

Panel type `volkovlabs-echarts-panel` (**Business Charts** plugin): an Apache ECharts chart driven by JavaScript.
Grafana's built-in Geomap can't animate, which is why this panel type is used.

* Query A: `/animation?group=..&ids=..&from=${__from}&to=${__to}`. It returns one row per satellite per time
  step. The step is at least 30 s and at most 10 min, chosen so the response stays small. The API refuses
  ranges over 24 h, and requests that would be too heavy (e.g. all of Starlink over more than ~2 h). The
  panel then shows the API's message instead of a map.
* `options.map: "json"` loads the world map that ships with the plugin.
* `options.getOption` holds the JavaScript that builds and animates the chart. Its readable, commented
  source is `grafana/panels/satellite-replay.js`. **Edit that file**, then run
  `python3 grafana/embed_panel_code.py` to copy it into this JSON.

What the code does:
1. It groups the rows by satellite.
2. It plays from the range's start to its end in `$anim_seconds` seconds, at about 25 frames per second.
   Positions between steps are interpolated along great circles.
3. It draws three layers: night shading computed from the Sun's position at the animated time; the path
   travelled so far (25 or fewer satellites); and the satellites, named when there are `$label_limit` or fewer.
4. It stops on the end time. On auto-refresh with the same selection and range length it doesn't replay: it
   just shows the new end positions. The ▶ button replays.

### 4. Sky view: "Sky above ${city:text}" (middle left)

Also a Business Charts panel. Its code is `grafana/panels/sky-view.js`, embedded the same way as panel 1.
`${city:text}` in the title shows the city's label rather than its key.

| refId | URL | Returns |
|---|---|---|
| A | `/sky?...&from=${__from}&to=${__to}&city=..&coords=..&min_el=..` | Satellites while above the horizon: one row per 20 s step (longer for ranges over 5 h), plus one step before rising and after setting |
| B | `/observer?city=..&coords=..` | Observer name, latitude, longitude |
| C | `/sky_chart` | Constellation lines and labels in RA/Dec |

The code uses a plain x/y chart with hidden axes. Altitude/azimuth is projected so that the zenith is at
(0, 0) and the horizon at radius 1, with North up and East left. The plot area is kept square so the sky
stays round.

For each animation frame it computes:
* the local sidereal time;
* the equatorial grid and constellation lines converted from RA/Dec to alt/az, split where they dip below
  the horizon;
* the Sun's position and the sky colour;
* the satellites, interpolated between steps along great circles. Colour shows sunlit vs in Earth's shadow.

Replay and auto-refresh behave as in panel 1.

### 5. Table: "Above the horizon at the end time" (middle right)

`/visible?...&t=${__to}&city=..&coords=..&min_el=..`: satellites above the minimum elevation, highest first.
The elevation column turns green above 30° and yellow above 60°.

### 2. Table: "Current positions" (bottom left)

Query A again. `fieldConfig.overrides` rename the API's column names (`alt_km` → "Altitude") and set units
(`lengthkm`, `degree`, `h`).

### 3. Stat: "Data status" (bottom right)

`/summary`: satellite count, median age of the orbital data, and minutes since the last download from
Space-Track. Thresholds colour the age orange above 72 h and red above 168 h.

## Layout

`gridPos` places each panel: the grid is 24 columns wide; `h` is in rows of about 30 px.
