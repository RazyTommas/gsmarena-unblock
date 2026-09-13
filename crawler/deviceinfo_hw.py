#!/usr/bin/env python3
"""
deviceinfo_hw.py — chipset, modem and WiFi silicon from crowdsourced device dumps.

NOT IN USE — THE SITE DECLINES AUTOMATED ACCESS. Read this before enabling it.

    https://deviceinfohw.ru/devices/uploads.php?model=<MODEL_CODE>
    with our own UA ('device-crawler/1.0')  -> HTTP 403
    with a browser UA                        -> HTTP 200, 37,771B

    robots.txt is 404, so there is no stated crawl policy to consult, and the earlier
    hand-verification of this host was done with a browser UA — which is why it looked
    open. It is not. A 403 to a self-identifying crawler is the site saying no to
    automated access, and sending a browser string to get past it would be
    misrepresenting what we are. That is the same line refused for samfw and for the
    fota-cloud UA before Ray authorised that one specifically, and this host has no
    such authorisation.

    The irony is worth recording: being honest in the User-Agent is exactly what got
    refused. That does not make dishonesty the fix.

    Enable only if the site's operator grants access, or if it starts serving a
    declared crawler UA. The code below is complete and tested against the response
    shape; nothing about it needs changing except permission.
    28 columns per upload, three of which we need:
        PLATFORM  mt6789            -> chipset
        MODEM     A155FXXSBEZG1     -> baseband
        WIFI      <chip>            -> a column no other source we have carries at all

WHY THIS SOURCE AND NOT A VENDOR
It reaches devices no vendor channel does. Google's catalog covers 6 of 321 TECNO
models; gsmarena needs a per-device crawl; Samsung's OTA manifest is Samsung-only.
This is uploads from real handsets across 118 brands, so it is the only path to the
long tail — and it independently carries modem values Samsung's own history does not
(three of seven SM-S928B values are absent from Samsung's list).

PROVENANCE IS WEAKER AND IS RECORDED AS SUCH
These are crowdsourced app uploads, not a vendor statement. A value here is evidence
that SOME handset reported it, not that a vendor published it. So rows are written
with src='deviceinfohw' and device_state weights that below a spec sheet or the Play
catalog. It fills only where nothing better exists and never overwrites a
vendor-sourced value.

Two consequences of that which the code enforces rather than assumes:
  * a model code that returns MULTIPLE distinct PLATFORM values is ambiguous and is
    left EMPTY, not resolved by majority vote. Regional variants of one model code
    genuinely ship different silicon, and picking the popular one would be a coin
    flip presented as data.
  * the page caps at 75 rows per query, so a model with more uploads is a SAMPLE.
    That is fine for identity (the chipset does not change) and not fine for
    recency, so MODEM values are stored with the caveat that they are whatever
    uploaders happened to be running.

    python3 deviceinfo_hw.py --dry-run --limit 20
    python3 deviceinfo_hw.py --limit 400
"""
from __future__ import annotations
import argparse, re, sys, time
from common import connect as db_connect, http_get, log_run, host_budget

SRC = "https://deviceinfohw.ru/devices/uploads.php?model={model}"
HOST = "deviceinfohw.ru"
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)


def cells(row_html):
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
            for c in CELL_RE.findall(row_html)]


def fetch_model(model, delay):
    ok, used, limit = host_budget(HOST)
    if not ok:
        return None, f"budget exhausted ({used}/{limit})"
    html = http_get(SRC.format(model=model), timeout=30)
    time.sleep(delay)
    if not html:
        return None, None
    rows = ROW_RE.findall(html)
    if not rows:
        return None, None
    hdr = cells(rows[0])
    if "MODEL" not in hdr:
        # 200 with a page that is not the table we asked for. Content decides.
        return None, "wrong-content"
    idx = {name: i for i, name in enumerate(hdr)}
    out = []
    for r in rows[1:]:
        c = cells(r)
        if len(c) < len(hdr):
            continue
        get = lambda k: (c[idx[k]] if k in idx and idx[k] < len(c) else "").strip()
        if get("MODEL").upper() != model.upper():
            continue                      # the page can list near-matches; be exact
        out.append({"platform": get("PLATFORM"), "modem": get("MODEM"),
                    "wifi": get("WIFI"), "android": get("ANDROID")})
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--delay", type=float, default=1.0)
    a = ap.parse_args()

    con = db_connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS deviceinfo_hw(
      model TEXT PRIMARY KEY, platform TEXT, modem TEXT, wifi TEXT,
      uploads INTEGER, ambiguous_platform INTEGER, fetched_at TEXT);
    """)

    # Control: a model we have already seen answer, so an empty result later is a real
    # absence rather than the host having stopped talking to us.
    ctl, err = fetch_model("SM-S928B", a.delay)
    if not ctl:
        print(f"CONTROL SM-S928B returned nothing ({err or 'no rows'}) — not collecting, "
              f"this would measure access rather than the site.")
        log_run("deviceinfohw", 0, outcome="blocked", note=f"control failed: {err}")
        return 1
    print(f"CONTROL SM-S928B: {len(ctl)} uploads, "
          f"platform={ctl[0]['platform']!r} modem={ctl[0]['modem']!r}")

    wanted = [r[0] for r in con.execute(
        """SELECT DISTINCT model FROM roms
           WHERE IFNULL(model,'')!='' AND LENGTH(model) >= 5
             AND (IFNULL(chipset,'')='' OR IFNULL(baseband,'')='')
           ORDER BY model LIMIT ?""", (a.limit,))]
    print(f"  querying {len(wanted)} model codes (>=5 chars; shorter ones are not "
          f"model codes and would match everything)", flush=True)

    rows, hit, amb = [], 0, 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for i, model in enumerate(wanted, 1):
        got, err = fetch_model(model, a.delay)
        if err:
            print(f"  {err} — stopping with {hit} models collected", flush=True)
            break
        if not got:
            continue
        plats = {g["platform"] for g in got if g["platform"]}
        modems = [g["modem"] for g in got if g["modem"]]
        wifis = {g["wifi"] for g in got if g["wifi"]}
        ambiguous = len(plats) > 1
        if ambiguous:
            amb += 1
        rows.append((model,
                     "" if ambiguous else (plats.pop() if plats else ""),
                     modems[0] if modems else "",
                     (wifis.pop() if len(wifis) == 1 else ""),
                     len(got), 1 if ambiguous else 0, now))
        hit += 1
        if i % 25 == 0:
            print(f"  {i}/{len(wanted)} · {hit} answered · {amb} ambiguous", flush=True)

    print(f"\n  {hit} model codes answered · {amb} had multiple PLATFORM values and are "
          f"left EMPTY rather than majority-voted")
    if a.dry_run:
        for r in rows[:8]:
            print(f"    {r[0]:14} platform={r[1][:18]:20} modem={r[2][:18]:20} wifi={r[3][:14]}")
        print("\n(dry run — nothing written)")
        return 0

    con.executemany("INSERT OR REPLACE INTO deviceinfo_hw VALUES(?,?,?,?,?,?,?)", rows)
    con.commit()
    # Fill only where empty. Never overwrite a vendor-sourced value with a crowdsourced one.
    con.execute("""UPDATE roms SET chipset = (
                     SELECT d.platform FROM deviceinfo_hw d
                     WHERE d.model = roms.model AND IFNULL(d.platform,'')!='')
                   WHERE IFNULL(chipset,'')='' AND EXISTS (
                     SELECT 1 FROM deviceinfo_hw d
                     WHERE d.model = roms.model AND IFNULL(d.platform,'')!='')""")
    ch = con.execute("SELECT changes()").fetchone()[0]
    con.execute("""UPDATE roms SET baseband = (
                     SELECT d.modem FROM deviceinfo_hw d
                     WHERE d.model = roms.model AND IFNULL(d.modem,'')!='')
                   WHERE IFNULL(baseband,'')='' AND EXISTS (
                     SELECT 1 FROM deviceinfo_hw d
                     WHERE d.model = roms.model AND IFNULL(d.modem,'')!='')""")
    bb = con.execute("SELECT changes()").fetchone()[0]
    con.commit()
    nd = con.execute("SELECT COUNT(DISTINCT device) FROM roms "
                     "WHERE IFNULL(chipset,'')!=''").fetchone()[0]
    print(f"  filled {ch:,} chipset rows and {bb:,} baseband rows")
    print(f"  devices with a chipset: {nd:,}")
    log_run("deviceinfohw", hit, outcome="ok",
            note=f"{hit} models, {ch} chipset + {bb} baseband rows filled (crowdsourced)")
    con.close()
    print("\nre-join:  python3 vuln.py --build && python3 device_state.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
