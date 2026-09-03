import json, urllib.request, urllib.error, time
BASE="http://localhost:3002"; S=open("/tmp/mb_session").read().strip()
def api(path, data=None, method=None):
    hdr={"Content-Type":"application/json","X-Metabase-Session":S}
    req=urllib.request.Request(BASE+path, data=(json.dumps(data).encode() if data is not None else None),
        headers=hdr, method=method or ("POST" if data is not None else "GET"))
    try: return json.loads(urllib.request.urlopen(req, timeout=40).read() or "{}")
    except urllib.error.HTTPError as e: print("HTTP",e.code,path,e.read().decode()[:400]); raise

# add sqlite database
try:
    db=api("/api/database", {"name":"Device Atlas","engine":"sqlite","details":{"db":"/data/devices.db"},"is_full_sync":True})
    dbid=db["id"]; print("added Device Atlas db id",dbid)
except urllib.error.HTTPError:
    dbs=api("/api/database"); lst=dbs["data"] if isinstance(dbs,dict) else dbs
    dbid=[d["id"] for d in lst if d["name"]=="Device Atlas"][0]; print("existing db id",dbid)
time.sleep(3)  # let it register

def card(name, sql, display="table", viz=None):
    c=api("/api/card", {"name":name,"dataset_query":{"type":"native","native":{"query":sql,"template-tags":{}},
        "database":dbid},"display":display,"visualization_settings":viz or {}})
    print("card:",name,"->",c["id"]); return c["id"]

VEND="substr(name,1,instr(name||' ',' ')-1)"
ids=[]
ids.append(card("Devices (searchable)",
    "SELECT name, \"Launch — Announced\" AS announced, \"Platform — Chipset\" AS chipset, "
    "\"Platform — OS\" AS os, \"Battery — Type\" AS battery, rom_count FROM devices ORDER BY name","table"))
ids.append(card("Firmware builds by source",
    "SELECT source, count(*) AS builds FROM roms GROUP BY source ORDER BY builds DESC","bar",
    {"graph.dimensions":["source"],"graph.metrics":["builds"]}))
ids.append(card("Chipset by vendor (native pivot)",
    f"SELECT \"Platform — Chipset\" AS chipset, {VEND} AS vendor, count(*) AS devices "
    f"FROM devices GROUP BY 1,2 ORDER BY devices DESC","bar",
    {"graph.dimensions":["chipset","vendor"],"graph.metrics":["devices"],"stackable.stack_type":"stacked"}))
ids.append(card("Firmware builds by region",
    "SELECT region, count(*) AS builds FROM roms WHERE region!='' GROUP BY region ORDER BY builds DESC LIMIT 12","row",
    {"graph.dimensions":["region"],"graph.metrics":["builds"]}))

dash=api("/api/dashboard", {"name":"Device Atlas — Exploration (Metabase)"}); did=dash["id"]
pos=[(0,0,12,7),(12,0,12,7),(0,7,12,8),(12,7,12,8)]
cards=[{"id":-(i+1),"card_id":cid,"col":x,"row":y,"size_x":w,"size_y":h,"parameter_mappings":[],"visualization_settings":{}}
       for i,(cid,(x,y,w,h)) in enumerate(zip(ids,pos))]
api(f"/api/dashboard/{did}/cards", {"cards":cards}, method="PUT")
print("DASHBOARD_URL", f"{BASE}/dashboard/{did}")
