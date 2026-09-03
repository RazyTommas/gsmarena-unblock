import json, urllib.request, urllib.error, time
BASE="http://localhost:3002"
def api(path, data=None, method=None, session=None):
    url=BASE+path
    hdr={"Content-Type":"application/json"}
    if session: hdr["X-Metabase-Session"]=session
    req=urllib.request.Request(url, data=(json.dumps(data).encode() if data is not None else None),
                               headers=hdr, method=method or ("POST" if data is not None else "GET"))
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read() or "{}")
    except urllib.error.HTTPError as e:
        print("HTTP",e.code,path,e.read().decode()[:300]); raise

# 1) setup token
tok=api("/api/session/properties")["setup-token"]
print("setup-token:", (tok or "already-setup"))
if tok:
    res=api("/api/setup", {"token":tok,
        "user":{"first_name":"Ray","last_name":"Lab","email":"ray@lab.local","password":"AtlasLab!234","site_name":"Device Atlas"},
        "prefs":{"site_name":"Device Atlas","allow_tracking":False},
        "database":{"engine":"sqlite","name":"Device Atlas","details":{"db":"/data/devices.db"}}})
    session=res["id"]; print("admin created, session ok")
else:
    session=api("/api/session",{"username":"ray@lab.local","password":"AtlasLab!234"})["id"]
open("/tmp/mb_session","w").write(session)

# 2) find db id
dbs=api("/api/database", session=session)
lst=dbs["data"] if isinstance(dbs,dict) and "data" in dbs else dbs
dbid=[d["id"] for d in lst if d["name"]=="Device Atlas"][0]
print("db id:", dbid)

def card(name, sql, display="table", viz=None):
    body={"name":name,"dataset_query":{"type":"native","native":{"query":sql,"template-tags":{}},"database":dbid},
          "display":display,"visualization_settings":viz or {}}
    c=api("/api/card", body, session=session); print("card:",name,"id",c["id"]); return c["id"]

VEND="substr(name,1,instr(name||' ',' ')-1)"
ids=[]
ids.append(card("Devices (searchable)",
    "SELECT name, \"Launch — Announced\" AS announced, \"Platform — Chipset\" AS chipset, "
    "\"Platform — OS\" AS os, \"Battery — Type\" AS battery, rom_count FROM devices ORDER BY name","table"))
ids.append(card("Firmware builds by source",
    "SELECT source, count(*) AS builds FROM roms GROUP BY source ORDER BY builds DESC","bar",
    {"graph.dimensions":["source"],"graph.metrics":["builds"]}))
ids.append(card("Chipset by vendor",
    f"SELECT \"Platform — Chipset\" AS chipset, {VEND} AS vendor, count(*) AS devices "
    f"FROM devices GROUP BY 1,2 ORDER BY devices DESC","bar",
    {"graph.dimensions":["chipset","vendor"],"graph.metrics":["devices"],"stackable.stack_type":"stacked"}))
ids.append(card("Firmware builds by region",
    "SELECT region, count(*) AS builds FROM roms WHERE region!='' GROUP BY region ORDER BY builds DESC","row",
    {"graph.dimensions":["region"],"graph.metrics":["builds"]}))

# 3) dashboard
dash=api("/api/dashboard", {"name":"Device Atlas — Exploration (Metabase)"}, session=session)
did=dash["id"]; print("dashboard id:", did)
dashcards=[]
pos=[(0,0,12,7),(12,0,12,7),(0,7,12,8),(12,7,12,8)]
for (cid,(x,y,w,h)) in zip(ids,pos):
    dashcards.append({"id":-(len(dashcards)+1),"card_id":cid,"col":x,"row":y,"size_x":w,"size_y":h,
                      "parameter_mappings":[],"visualization_settings":{}})
api(f"/api/dashboard/{did}/cards", {"cards":dashcards}, session=session, method="PUT")
print("DASHBOARD_URL", f"{BASE}/dashboard/{did}")
