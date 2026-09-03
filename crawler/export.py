#!/usr/bin/env python3
"""
export.py — turn crawled JSON into a searchable SQLite DB + CSV files.

Reads output/**/*.json and produces:
  data/devices.db   SQLite with two tables:
                      devices  — gsmarena specs, one row per device, spec fields
                                 flattened to columns ("Network — Technology", …)
                      roms     — mifirm firmware, one row per ROM build, with a
                                 direct download link and the model page link
  data/devices.csv  flattened device specs
  data/roms.csv     one row per ROM build

Run:  python export.py            (reads ./output, writes ./data)
      python export.py --out other_dir --data mydata
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from pathlib import Path


# top-level manufacturer words to strip (sub-brands like redmi/poco/mi are KEPT,
# because they carry the model identity shared across gsmarena and mifirm names)
MANUFACTURERS = {"xiaomi", "samsung", "apple", "google", "oneplus", "motorola",
                 "huawei", "honor", "oppo", "vivo", "realme", "nokia", "sony",
                 "asus", "lenovo", "zte", "nothing", "tecno", "infinix", "itel"}


def norm_key(name: str) -> str:
    """Loose normalized key (drops radios) — kept for reference/debugging."""
    s = (name or "").lower()
    s = re.sub(r"\b5g\b|\b4g\b|\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def norm_code(code: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (code or "").lower())


def model_codes(text: str) -> set[str]:
    """Extract device model codes (SM-S741B, X6895, RMX3999 …) from free text."""
    out = set()
    for m in re.findall(r"\bSM-[A-Z0-9]+\b|\b[A-Z]{2,4}\d{3,5}[A-Z0-9]*\b",
                        (text or "").upper()):
        code = norm_code(m.split("/")[0])
        if len(code) >= 4:
            out.add(code)
    return out


def join_keys(name: str) -> set[str]:
    """
    Identity keys used to relate a gsmarena device to a mifirm model.
    Splits '/'-bundled names into segments; strips the manufacturer word and '5G'
    (keeps '4G', which distinguishes real variants). e.g.
      'Xiaomi Redmi Note 17 Pro Max'          -> {'redmi note 17 pro max'}
      'Redmi Note 17 Pro Max 5G / POCO X8 Pw' -> {'redmi note 17 pro max', 'poco x8 pw'}
    """
    text = re.sub(r"\(.*?\)", " ", (name or "").lower())
    keys = set()
    for seg in re.split(r"\s*/\s*", text):
        toks = [t for t in re.sub(r"[^a-z0-9]+", " ", seg).split()
                if t not in MANUFACTURERS and t != "5g"]
        key = " ".join(toks)
        if len(key) >= 3:            # skip weak keys like bare "17"
            keys.add(key)
    return keys


def load_json(out_dir: Path) -> tuple[list[dict], list[dict]]:
    devices, roms = [], []
    dev_meta = []   # parallel to devices: {device_id, keys}
    rom_meta = []   # parallel to roms: {keys}

    for fp in sorted(out_dir.rglob("*.json")):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        src = d.get("source", "")
        if src == "mifirm.net":
            mkeys = join_keys(d.get("name"))
            for r in d.get("roms", []):
                roms.append({
                    "source": "mifirm.net",
                    "device": d.get("name"),
                    "codename": d.get("codename"),
                    "model": None,
                    "region": r.get("region"),
                    "type": r.get("type"),
                    "branch": r.get("branch"),
                    "version": r.get("version"),
                    "android": r.get("android"),
                    "size": r.get("size"),
                    "updated_at": r.get("updated_at"),
                    "downloads": r.get("downloads"),
                    "download_url": r.get("download_url"),
                    "model_url": d.get("url"),
                    "matched_devices": None,
                })
                rom_meta.append(mkeys)
        elif src in ("samfw.com", "givemerom.com"):
            keys = {norm_code(d.get("model"))} if d.get("model") else set()
            keys |= join_keys(d.get("name"))
            for r in d.get("roms", []):
                bd = r.get("build_date") or ""
                date = f"{bd[:4]}-{bd[4:6]}-{bd[6:8]}" if len(bd) >= 8 and bd.isdigit() else bd
                roms.append({
                    "source": src,
                    "device": d.get("name"),
                    "codename": None,
                    "model": d.get("model"),
                    "region": r.get("region"),          # CSC = region/carrier
                    "type": "stock",
                    "branch": (f"OneUI {r['oneui']}" if r.get("oneui")
                               else ("Samsung" if src == "samfw.com" else "stock")),
                    "version": r.get("version"),
                    "android": r.get("android"),
                    "size": None,
                    "updated_at": date,
                    "downloads": None,
                    "download_url": r.get("download_url"),
                    "model_url": d.get("url"),
                    "matched_devices": None,
                })
                rom_meta.append(keys)
        elif src == "firmwarefile.com":
            keys = {norm_code(d.get("model"))} if d.get("model") else set()
            keys |= join_keys(d.get("name"))
            roms.append({
                "source": "firmwarefile.com",
                "device": d.get("name"),
                "codename": None,
                "model": d.get("model"),
                "region": "Multi",
                "type": "firmware",
                "branch": (d.get("brand") or "").title() or None,
                "version": d.get("build"),
                "android": d.get("android"),
                "size": d.get("size"),
                "updated_at": None,
                "downloads": len(d.get("downloads", [])),
                "download_url": d.get("download_url"),
                "model_url": d.get("url"),
                "matched_devices": None,
            })
            rom_meta.append(keys)
        elif src == "romprovider.com":
            keys = {norm_code(d.get("model"))} if d.get("model") else set()
            keys |= join_keys(d.get("name"))
            roms.append({
                "source": "romprovider.com",
                "device": d.get("name"),
                "codename": None,
                "model": d.get("model"),
                "region": "Multi",
                "type": "firmware",
                "branch": (d.get("brand") or "").title() or None,
                "version": d.get("version"),
                "android": d.get("android"),
                "size": None,
                "updated_at": None,
                "downloads": None,
                "download_url": d.get("download_url"),
                "model_url": d.get("url"),
                "matched_devices": None,
            })
            rom_meta.append(keys)
        else:  # gsmarena device
            device_id = str(d.get("gsmarena_id") or f"g{len(devices)}")
            row = {
                "device_id": device_id,
                "name": d.get("name"),
                "url": d.get("url"),
                "image": d.get("image"),
                "rom_count": 0,
                "codenames": None,
                "rom_url": None,
            }
            for section, fields in (d.get("sections") or {}).items():
                for k, v in fields.items():
                    row[f"{section} — {k}"] = v
            devices.append(row)
            keys = join_keys(d.get("name"))
            keys |= model_codes(row.get("Misc — Models", "") + " " + (d.get("name") or ""))
            # also index each explicit model/codename token (catches short codes
            # like Tecno CM6 / KM5 / BE8 that the SM-style matcher misses)
            for tok in re.split(r"[,\s/]+", row.get("Misc — Models", "") or ""):
                nc = norm_code(tok)
                if len(nc) >= 3:
                    keys.add(nc)
            dev_meta.append({"id": device_id, "keys": keys})

    # ---- the join: a ROM belongs to every device sharing a key ---- #
    for ri, rkeys in enumerate(rom_meta):
        matched = [m["id"] for m in dev_meta if rkeys & m["keys"]]
        roms[ri]["matched_devices"] = ",".join(matched) if matched else ""
    for di, meta in enumerate(dev_meta):
        mine = [roms[ri] for ri in range(len(roms))
                if meta["id"] in (roms[ri]["matched_devices"] or "").split(",")]
        devices[di]["rom_count"] = len(mine)
        tags = sorted({r["codename"] or r["model"] for r in mine if r["codename"] or r["model"]})
        devices[di]["codenames"] = ", ".join(tags) if tags else None
        devices[di]["rom_url"] = mine[0]["model_url"] if mine else None

    matched_dev = sum(1 for d in devices if d["rom_count"])
    matched_rom = sum(1 for r in roms if r["matched_devices"])
    print(f"[join] {matched_dev}/{len(devices)} devices linked to ROMs; "
          f"{matched_rom}/{len(roms)} ROM builds linked to a device")
    return devices, roms


def union_columns(rows: list[dict], lead: list[str]) -> list[str]:
    cols = list(lead)
    seen = set(lead)
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols


def write_csv(path: Path, cols: list[str], rows: list[dict]):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def make_table(conn: sqlite3.Connection, table: str, cols: list[str], rows: list[dict]):
    q = lambda c: '"' + c.replace('"', '""') + '"'
    conn.execute(f"DROP TABLE IF EXISTS {q(table)}")
    conn.execute(f"CREATE TABLE {q(table)} ({', '.join(q(c) + ' TEXT' for c in cols)})")
    placeholders = ", ".join("?" for _ in cols)
    conn.executemany(
        f"INSERT INTO {q(table)} ({', '.join(q(c) for c in cols)}) VALUES ({placeholders})",
        [[_stringify(r.get(c)) for c in cols] for r in rows],
    )
    conn.commit()


def _stringify(v):
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Derived analytics layer — clean, typed fields so an agent/BI tool can query  #
# freely without parsing messy gsmarena strings in SQL.                        #
# --------------------------------------------------------------------------- #
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
_MONTHS.update({"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
                "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12})
_VENDOR_ALIAS = {"mi": "Xiaomi", "mix": "Xiaomi", "redmi": "Redmi",
                 "poco": "Poco", "pocophone": "Poco"}


def a_vendor(name):
    w = (name or "").split()
    if not w:
        return None
    k = w[0].lower()
    return _VENDOR_ALIAS.get(k, w[0][:1].upper() + w[0][1:])


def a_chipset_short(s):
    s = s or ""
    for p in (r"Snapdragon\s+[\w\s]+?(?:Gen\s*\d+|Elite(?:\s+Gen\s*\d+)?|\d[\w+]*)",
              r"Dimensity\s+\d+\w*", r"Helio\s+\w+", r"Exynos\s+\w+",
              r"Tensor\s*\w*", r"Unisoc\s+\w+", r"Kirin\s+\w+"):
        m = re.search(p, s, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()
    w = re.sub(r"\(.*?\)", "", s).strip().split()
    return " ".join(w[:3]) or None


def _max_int(pat, s):
    vals = [int(x) for x in re.findall(pat, s or "", re.I)]
    return max(vals) if vals else None


def a_ram_gb(s):     return _max_int(r"(\d+)\s*GB\s*RAM", s)
def a_storage_gb(s): return _max_int(r"(\d+)\s*GB(?!\s*RAM)", s)
def a_battery(s):
    m = re.search(r"(\d{3,5})\s*mAh", s or "", re.I); return int(m.group(1)) if m else None
def a_android(s):
    m = re.search(r"Android\s*(\d{1,2})", s or "", re.I); return int(m.group(1)) if m else None
def a_display_in(s):
    m = re.search(r"([\d.]+)\s*inches", s or ""); return float(m.group(1)) if m else None
def a_price_eur(s):
    m = re.search(r"€\s*([\d.,]+)", s or "")
    return float(m.group(1).replace(",", "")) if m else None
def a_date(s, iso=True):
    if not s:
        return None
    m = re.search(r"(\d{4})(?:[,\s]+([A-Za-z]+))?(?:\s+(\d{1,2}))?", str(s))
    if not m:
        return None
    y = int(m.group(1)); mo = _MONTHS.get((m.group(2) or "").lower(), 1); d = int(m.group(3) or 1)
    return f"{y:04d}-{mo:02d}-{d:02d}"
def a_norm_android(s):
    m = re.search(r"(\d{1,2})", str(s or "")); return int(m.group(1)) if m else None


def build_analytics(conn, devices, roms):
    """Create typed, clean tables: v_devices, v_roms, v_device_firmware (joined)."""
    q = lambda c: '"' + c.replace('"', '""') + '"'

    def typed_table(name, cols, rows):
        conn.execute(f"DROP TABLE IF EXISTS {q(name)}")
        conn.execute(f"CREATE TABLE {q(name)} ({', '.join(q(c)+' '+t for c, t in cols)})")
        conn.executemany(
            f"INSERT INTO {q(name)} ({', '.join(q(c) for c, _ in cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [[r.get(c) for c, _ in cols] for r in rows])
        conn.commit()

    dev_rows, by_id = [], {}
    for d in devices:
        row = {
            "device_id": d.get("device_id"), "name": d.get("name"),
            "vendor": a_vendor(d.get("name")),
            "announced": a_date(d.get("Launch — Announced")),
            "announced_year": (a_date(d.get("Launch — Announced")) or "0000")[:4],
            "chipset": d.get("Platform — Chipset"),
            "chipset_short": a_chipset_short(d.get("Platform — Chipset")),
            "os": d.get("Platform — OS"),
            "android": a_android(d.get("Platform — OS")),
            "ram_gb": a_ram_gb(d.get("Memory — Internal")),
            "storage_gb": a_storage_gb(d.get("Memory — Internal")),
            "battery_mah": a_battery(d.get("Battery — Type") or d.get("Battery — Charging")),
            "display_in": a_display_in(d.get("Display — Size")),
            "price_eur": a_price_eur(d.get("Misc — Price")),
            "rom_count": int(d.get("rom_count") or 0),
            "codenames": d.get("codenames"), "url": d.get("url"), "image": d.get("image"),
        }
        dev_rows.append(row)
        by_id[row["device_id"]] = row
    typed_table("v_devices", [
        ("device_id", "TEXT"), ("name", "TEXT"), ("vendor", "TEXT"), ("announced", "TEXT"),
        ("announced_year", "TEXT"), ("chipset", "TEXT"), ("chipset_short", "TEXT"),
        ("os", "TEXT"), ("android", "INTEGER"), ("ram_gb", "INTEGER"), ("storage_gb", "INTEGER"),
        ("battery_mah", "INTEGER"), ("display_in", "REAL"), ("price_eur", "REAL"),
        ("rom_count", "INTEGER"), ("codenames", "TEXT"), ("url", "TEXT"), ("image", "TEXT")], dev_rows)

    rom_rows, joined = [], []
    for i, r in enumerate(roms):
        rr = {
            "rom_id": i, "source": r.get("source"), "device": r.get("device"),
            "vendor": a_vendor(r.get("device")), "model": r.get("model"),
            "codename": r.get("codename"), "region": r.get("region"), "type": r.get("type"),
            "branch": r.get("branch"), "version": r.get("version"),
            "android": a_norm_android(r.get("android")), "size": r.get("size"),
            "release_date": a_date(r.get("updated_at")) if r.get("updated_at") else None,
            "release_year": (a_date(r.get("updated_at")) or "")[:4] or None,
            "downloads": (int(r["downloads"]) if str(r.get("downloads") or "").isdigit() else None),
            "download_url": r.get("download_url"), "model_url": r.get("model_url"),
        }
        rom_rows.append(rr)
        for did in (r.get("matched_devices") or "").split(","):
            dev = by_id.get(did)
            if dev:
                joined.append({**{f"dev_{k}": v for k, v in dev.items()},
                               **{f"fw_{k}": v for k, v in rr.items()}})
    typed_table("v_roms", [
        ("rom_id", "INTEGER"), ("source", "TEXT"), ("device", "TEXT"), ("vendor", "TEXT"),
        ("model", "TEXT"), ("codename", "TEXT"), ("region", "TEXT"), ("type", "TEXT"),
        ("branch", "TEXT"), ("version", "TEXT"), ("android", "INTEGER"), ("size", "TEXT"),
        ("release_date", "TEXT"), ("release_year", "TEXT"), ("downloads", "INTEGER"),
        ("download_url", "TEXT"), ("model_url", "TEXT")], rom_rows)

    jcols = ([("dev_" + c, t) for c, t in [
        ("device_id", "TEXT"), ("name", "TEXT"), ("vendor", "TEXT"), ("announced", "TEXT"),
        ("chipset_short", "TEXT"), ("android", "INTEGER"), ("ram_gb", "INTEGER"),
        ("battery_mah", "INTEGER"), ("price_eur", "REAL")]] +
        [("fw_" + c, t) for c, t in [
        ("source", "TEXT"), ("region", "TEXT"), ("type", "TEXT"), ("branch", "TEXT"),
        ("version", "TEXT"), ("android", "INTEGER"), ("release_date", "TEXT"),
        ("download_url", "TEXT")]])
    typed_table("v_device_firmware", jcols, joined)
    print(f"[analytics] v_devices {len(dev_rows)}, v_roms {len(rom_rows)}, "
          f"v_device_firmware {len(joined)} (typed, clean fields)")


def main():
    ap = argparse.ArgumentParser(description="Export crawled JSON to SQLite + CSV.")
    ap.add_argument("--out", default="output", help="crawler output dir to read")
    ap.add_argument("--data", default="data", help="destination dir for db/csv")
    args = ap.parse_args()

    out_dir, data_dir = Path(args.out), Path(args.data)
    data_dir.mkdir(parents=True, exist_ok=True)

    devices, roms = load_json(out_dir)
    dev_cols = union_columns(devices, ["device_id", "name", "rom_count", "codenames",
                                       "url", "rom_url", "image"])
    rom_cols = union_columns(roms, ["source", "device", "model", "codename", "region",
                                    "type", "branch", "version", "android", "size",
                                    "updated_at", "downloads", "download_url",
                                    "model_url", "matched_devices"])

    db = data_dir / "devices.db"
    conn = sqlite3.connect(db)
    make_table(conn, "devices", dev_cols, devices)
    make_table(conn, "roms", rom_cols, roms)
    build_analytics(conn, devices, roms)   # clean typed layer for agents/BI
    conn.close()

    write_csv(data_dir / "devices.csv", dev_cols, devices)
    write_csv(data_dir / "roms.csv", rom_cols, roms)

    print(f"[export] devices: {len(devices)} rows, {len(dev_cols)} columns")
    print(f"[export] roms:    {len(roms)} rows, {len(rom_cols)} columns")
    print(f"[export] wrote {db}, {data_dir/'devices.csv'}, {data_dir/'roms.csv'}")


if __name__ == "__main__":
    main()
