"""The captured source files `run_batch` reads out of `--legacy-root`, named once.

WHY THIS FILE EXISTS
tools/package_portable.py shipped the corpus, the local database, the ledger and
the evidence -- everything needed to SERVE -- and none of the batch's inputs. An
air-gapped box unpacked from such a bundle could answer questions about the
corpus it arrived with and could never get another one: `run_batch` reads these
files from `--legacy-root`, whose default points at a sibling checkout that does
not exist beside an unpacked bundle, and the second step of the batch
(`seed_samsung_history_identities`) opens one of them directly and raises
FileNotFoundError. The box was frozen at whatever corpus it shipped with.

The packager needs to know exactly which files those are, and that knowledge has
to live somewhere other than inside `run_batch`'s body, where nothing can read
it. It lives here.

DRIFT
This list is not derived from batch.py at import time -- the paths there are
built inline from string literals, and evaluating them would mean running the
batch. So it can go stale the moment someone wires a new source in. That is
checked rather than hoped for: `PackagedInputsMatchTheBatchTest` in
tests/test_portable_bundle.py parses batch.py and asserts that the set of
`legacy_root / ... / ...` expressions in it is exactly this tuple. Adding a
source without adding it here fails that test, which is the only place the two
can be compared.

REQUIRED MEANS REQUIRED
Every path below is opened unconditionally by a single batch run. There is no
"optional input" tier, because there is no behaviour in `run_batch` that skips a
source whose file is absent: an adapter whose fetch() raises is recorded as a
`failed` run and imports nothing, and the four direct readers (the Samsung
history seed, the identity bridge, the dedupe pass and the enrichment/catalog
steps) raise outright. A bundle missing any one of them is a bundle whose batch
either crashes or silently ingests less, so the packager refuses to build one.
"""
from __future__ import annotations

from pathlib import Path

#: Paths relative to `--legacy-root`, in the order `run_batch` first reads them.
REQUIRED_LEGACY_INPUTS: tuple[str, ...] = (
    "T005-fota-modem/samsung_fota.csv",
    "xiaomi-tracker/latest.yml",
    "tecno-security-comprehensive/tecno-security-updates.csv",
    "samsung-aspl/samsung_aspl.csv",
    "mifirm-archive/mifirm-firmware-archive.csv",
    "ipsw-me/ipsw-me-firmware.csv",
    "frbox-transsion-catalog/transsion_frbox_catalog.csv",
    "naijarom-transsion/naijarom-merged.csv",
    "tecno-ota-checkin/tecno_ota_checkin.csv",
    "infinix-itel-ota-checkin/infinix_itel_ota_checkin.csv",
    "T004-gsmarena-slugs/gsm_specs.csv",
    "xiaomi-tracker/devices.yml",
    "google-play-devices/supported_devices.csv",
    "cross-reference/device-chipset-cve-xref.csv",
    "google-asb-cves/android-security-bulletin-cves.csv",
    "mediatek-cve-chipsets/mediatek-cve-chipsets.csv",
)

#: Where the input set lives inside a packaged bundle.
BUNDLE_INPUTS_DIRECTORY = "inputs"


def missing_inputs(legacy_root: Path) -> list[str]:
    """Return the required paths that are not files under `legacy_root`.

    An empty list means the batch can run against this root. It does not mean
    the contents are correct -- only that nothing it opens is absent.
    """
    root = Path(legacy_root)
    return [name for name in REQUIRED_LEGACY_INPUTS if not (root / name).is_file()]
