#!/usr/bin/env python3
"""Build review-only profiles from captured Xiaomi and TECNO artifacts."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parents[1] / "fixtures" / "xiaomi_tecno_review_profiles.json"

XIAOMI = (
    ("Xiaomi Poco F9 Ultra", "flagship", ("songyuan_",)),
    ("Xiaomi Poco F9 Pro", "performance", ("athens_",)),
    ("Xiaomi Redmi Note 17 Pro Max", "mid-range", ("brussels_",)),
    ("Xiaomi Redmi A7 Pro", "entry", ("arctic_", "somalia_in_global")),
)
TECNO = (
    ("TECNO PHANTOM V Fold2 5G", "flagship/foldable"),
    ("TECNO CAMON 40 Pro 5G", "upper-mid-range"),
    ("TECNO POVA 7 5G", "performance"),
    ("TECNO SPARK 40C", "entry"),
)


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def split_names(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def main() -> None:
    specs = read(ROOT / "crawler/relay/results/T004-gsmarena-slugs/gsm_specs.csv")
    firmware = read(ROOT / "crawler/relay/results/xiaomi-tracker/xiaomi-firmware-latest.csv")
    patches = read(ROOT / "crawler/relay/results/tecno-security-comprehensive/tecno-security-updates.csv")
    profiles: list[dict] = []

    for name, tier, prefixes in XIAOMI:
        spec = next(row for row in specs if row["device_name"] == name)
        roms = [row for row in firmware if any(row["codename"].startswith(prefix) for prefix in prefixes)]
        profiles.append({
            "id": spec["slug"], "manufacturer": "Xiaomi", "tier": tier, "name": name,
            "source_identities": sorted({row["codename"] for row in roms}),
            "specification": {
                "chipset": spec["chipset"], "cpu": spec["cpu"], "gpu": spec["gpu"],
                "launch_os": spec["os"], "display": spec["displaysize"],
                "battery": spec["batdescription1"], "memory": spec["internalmemory"],
                "wifi": spec["wlan"], "bluetooth": spec["bluetooth"],
                "4g": spec["net4g"], "5g": spec["net5g"], "observed_at": spec["fetched_at"],
            },
            "firmware_history": roms,
            "review_state": "needs authoritative hardware model code and codename mapping",
            "identity_warning": "A codename is a source identity hint, not a canonical hardware model code.",
        })

    for name, tier in TECNO:
        history = [row for row in patches if name in split_names(row["device"])]
        profiles.append({
            "id": "tecno-source-name:" + name.lower().replace(" ", "-"),
            "manufacturer": "TECNO", "tier": tier, "name": name,
            "source_identities": [name], "security_patch_history": history,
            "review_state": "needs authoritative hardware model code and specification enrichment",
            "identity_warning": "Vendor display name is not sufficient to merge regional or hardware variants.",
            "coverage_warning": "Captured vendor feed provides patch month only; firmware, chipset and Android data are not present.",
        })

    OUT.write_text(json.dumps({
        "purpose": "review-only profiles; no record is a canonical identity",
        "generated_from": ["gsm_specs.csv", "xiaomi-firmware-latest.csv", "tecno-security-updates.csv"],
        "profiles": profiles,
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
