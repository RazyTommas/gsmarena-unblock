import json, urllib.request

DS = {"type": "frser-sqlite-datasource", "uid": "deviceatlas"}

def target(sql, qtype="table", timecols=None):
    return {"refId": "A", "datasource": DS, "queryText": sql, "rawQueryText": sql,
            "queryType": qtype, "timeColumns": timecols or []}

def stat(title, sql, x, w=4, color="blue"):
    return {"type": "stat", "title": title, "datasource": DS,
            "gridPos": {"h": 4, "w": w, "x": x, "y": 0},
            "targets": [target(sql)],
            "fieldConfig": {"defaults": {"color": {"mode": "fixed", "fixedColor": color},
                            "unit": "short"}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "none", "textMode": "value_and_name"}}

def barchart(title, sql, gp):
    return {"type": "barchart", "title": title, "datasource": DS, "gridPos": gp,
            "targets": [target(sql)],
            "fieldConfig": {"defaults": {"color": {"mode": "palette-classic"}}, "overrides": []},
            "options": {"orientation": "horizontal", "xTickLabelRotation": 0,
                        "showValue": "always", "stacking": "none", "legend": {"showLegend": False}}}

def timeseries(title, sql, gp):
    return {"type": "timeseries", "title": title, "datasource": DS, "gridPos": gp,
            "targets": [target(sql, "time series", ["time"])],
            "fieldConfig": {"defaults": {"color": {"mode": "palette-classic"},
                            "custom": {"drawStyle": "bars", "fillOpacity": 70, "stacking": {"mode": "normal"},
                                       "lineWidth": 1}}, "overrides": []},
            "options": {"legend": {"showLegend": True, "placement": "bottom"},
                        "tooltip": {"mode": "multi"}}}

def table(title, sql, gp):
    return {"type": "table", "title": title, "datasource": DS, "gridPos": gp,
            "targets": [target(sql)], "fieldConfig": {"defaults": {}, "overrides": []},
            "options": {"showHeader": True}}

# ---------- OPS dashboard ----------
ops_panels = [
    stat("Devices", "SELECT count(*) FROM devices", 0, 4, "blue"),
    stat("Firmware builds", "SELECT count(*) FROM roms", 4, 4, "purple"),
    stat("Sources", "SELECT count(DISTINCT source) FROM roms", 8, 4, "green"),
    stat("Linked devices", "SELECT count(*) FROM devices WHERE CAST(rom_count AS INT)>0", 12, 4, "orange"),
    stat("Regions", "SELECT count(DISTINCT region) FROM roms WHERE region!=''", 16, 4, "red"),
    stat("Newest build (days ago)", "SELECT CAST(julianday(date('now'))-julianday(max(updated_at)) AS INT) FROM roms WHERE updated_at!=''", 20, 4, "blue"),
    timeseries("Firmware builds per month (by source)",
        "SELECT CAST(strftime('%s', substr(updated_at,1,7)||'-01') AS INTEGER)*1000 AS time, "
        "SUM(source='mifirm.net') AS \"mifirm.net\", SUM(source='samfw.com') AS \"samfw.com\" "
        "FROM roms WHERE updated_at!='' AND length(updated_at)>=7 GROUP BY 1 ORDER BY 1",
        {"h": 9, "w": 16, "x": 0, "y": 4}),
    barchart("Builds by source", "SELECT source AS metric, count(*) AS builds FROM roms GROUP BY 1 ORDER BY 2 DESC",
        {"h": 9, "w": 8, "x": 16, "y": 4}),
    barchart("Android version spread",
        "SELECT 'Android '||CAST(CAST(android AS INT) AS TEXT) AS metric, count(*) AS builds FROM roms "
        "WHERE android GLOB '[0-9]*' GROUP BY 1 ORDER BY count(*) DESC LIMIT 12",
        {"h": 9, "w": 12, "x": 0, "y": 13}),
    table("Newest firmware builds",
        "SELECT updated_at AS released, device, source, region, version, android FROM roms "
        "WHERE updated_at!='' ORDER BY updated_at DESC LIMIT 25",
        {"h": 9, "w": 12, "x": 12, "y": 13}),
]
ops = {"dashboard": {"uid": "atlas-ops", "title": "Device Atlas — Ops / Health", "tags": ["atlas"],
       "timezone": "browser", "schemaVersion": 39, "refresh": "", "time": {"from": "now-5y", "to": "now"},
       "panels": ops_panels}, "folderUid": "", "overwrite": True}

# ---------- EXPLORATION dashboard (rebuild of custom-UI panels) ----------
VENDOR = "substr(name,1,instr(name||' ',' ')-1)"
VENDOR_R = "substr(device,1,instr(device||' ',' ')-1)"
expl_panels = [
    table("Devices (filtered by $vendor)",
        f"SELECT name, \"Launch — Announced\" AS announced, \"Platform — Chipset\" AS chipset, "
        f"\"Platform — OS\" AS os, \"Battery — Type\" AS battery, rom_count FROM devices "
        f"WHERE {VENDOR}='$vendor' OR '$vendor'='(all)' ORDER BY name",
        {"h": 10, "w": 24, "x": 0, "y": 0}),
    barchart("Chipset by count (a stacked-by-vendor pivot is where Grafana gets clumsy)",
        "SELECT \"Platform — Chipset\" AS metric, count(*) AS devices FROM devices "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 12", {"h": 10, "w": 12, "x": 0, "y": 10}),
    barchart("Firmware builds by region",
        "SELECT region AS metric, count(*) AS builds FROM roms WHERE region!='' GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
        {"h": 10, "w": 12, "x": 12, "y": 10}),
]
expl = {"dashboard": {"uid": "atlas-explore", "title": "Device Atlas — Exploration (Grafana rebuild)",
        "tags": ["atlas"], "timezone": "browser", "schemaVersion": 39, "refresh": "",
        "templating": {"list": [{"name": "vendor", "type": "query", "datasource": DS,
            "query": f"SELECT DISTINCT {VENDOR} FROM devices ORDER BY 1", "refresh": 1,
            "includeAll": True, "allValue": "(all)", "current": {"text": "Samsung", "value": "Samsung"}}]},
        "panels": expl_panels}, "overwrite": True}

for name, dash in [("ops", ops), ("explore", expl)]:
    req = urllib.request.Request("http://localhost:3003/api/dashboards/db",
        data=json.dumps(dash).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic YWRtaW46YWRtaW4="})  # admin:admin
    try:
        r = urllib.request.urlopen(req, timeout=15)
        print(name, "->", json.loads(r.read())["status"])
    except urllib.error.HTTPError as e:
        print(name, "ERROR", e.code, e.read().decode()[:300])
