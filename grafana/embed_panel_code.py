"""Copy the panel JavaScript files into the dashboard JSON.

The animated panels (Business Charts) store their JavaScript as a string in
satellites.json -> panel option "getOption". Editing JavaScript inside a JSON
string is painful, so edit the .js files in grafana/panels/ and run:

    python3 grafana/embed_panel_code.py

Grafana picks up the change within 30 s (see provisioning/dashboards/satellites.yml).
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
DASHBOARD = HERE / "dashboards" / "satellites.json"
# dashboard panel id -> JavaScript source file
PANEL_CODE = {
    1: HERE / "panels" / "satellite-replay.js",  # animated world map
    4: HERE / "panels" / "sky-view.js",          # sky above the observer
}

dash = json.loads(DASHBOARD.read_text())
for panel in dash["panels"]:
    src = PANEL_CODE.get(panel.get("id"))
    if src and panel.get("type") == "volkovlabs-echarts-panel":
        panel["options"]["getOption"] = src.read_text()
        print(f"panel {panel['id']} ({panel['title']}) <- {src.name}")
DASHBOARD.write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n")
