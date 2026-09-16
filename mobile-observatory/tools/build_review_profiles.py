#!/usr/bin/env python3
"""Build review-only full profiles from existing captured GSMArena/Xiaomi artifacts."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parents[1] / "fixtures" / "review_profiles.json"
CHOICES = [
    ("Xiaomi Poco F9 Ultra", ("songyuan_global", "songyuan_eea_global"), "flagship"),
    ("Xiaomi Redmi Note 17 Pro Max", ("brussels_global", "brussels_eea_global"), "mid-range"),
    ("Xiaomi Redmi A7 Pro", ("arctic_global", "arctic_eea_global"), "entry"),
]


def read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    specs = read(ROOT / "crawler/relay/results/T004-gsmarena-slugs/gsm_specs.csv")
    firmware = read(ROOT / "crawler/relay/results/xiaomi-tracker/xiaomi-firmware-latest.csv")
    profiles = []
    for name, codenames, tier in CHOICES:
        spec = next(row for row in specs if row["device_name"] == name)
        roms = [row for row in firmware if row["codename"] in codenames]
        profiles.append({
            "id": spec["slug"], "tier": tier, "name": name, "source_identity": list(codenames),
            "spec_observed_at": spec["fetched_at"], "chipset": spec["chipset"],
            "cpu": spec["cpu"], "gpu": spec["gpu"], "launch_os": spec["os"],
            "display": spec["displaysize"], "battery": spec["batdescription1"],
            "memory": spec["internalmemory"], "wifi": spec["wlan"], "bluetooth": spec["bluetooth"],
            "radio": {"4g": spec["net4g"], "5g": spec["net5g"]}, "firmware": roms,
            "review_state": "needs authoritative hardware model code",
        })
    OUT.write_text(json.dumps({"generated_from": ["gsm_specs.csv", "xiaomi-firmware-latest.csv"],
                               "profiles": profiles}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
