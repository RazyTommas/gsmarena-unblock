# Sampled legacy root (test fixture)

A complete-but-small `--legacy-root` tree: every one of the files listed in
`src/mobile_observatory/batch_inputs.py:REQUIRED_LEGACY_INPUTS`, and nothing
else. `tests/test_portable_bundle.py` packages this into a bundle, unpacks the
bundle somewhere fresh, and runs the batch against it.

## Why a sample and not the real tree

The real captured tree (`crawler/relay/results/`) is ~24 MB and a batch over it
takes minutes. This is the same files truncated, so a test can run the real
ingest path end to end in about 11 seconds.

## What these files are

Real captured content, not synthesised. Each file is the header row plus the
FIRST N data rows of the corresponding file under `crawler/relay/results/`,
sliced with Python's `csv` module so quoted multi-line fields stay intact.
`xiaomi-tracker/latest.yml` keeps its first 150 whole records and
`xiaomi-tracker/devices.yml` its first 200 whole codename blocks.

Row counts: samsung_fota 200, samsung_aspl 200, mifirm-firmware-archive 150,
tecno-security-updates 60, ipsw-me-firmware 100, transsion_frbox_catalog 120,
naijarom-merged 120, tecno_ota_checkin 30, infinix_itel_ota_checkin 30,
gsm_specs 80, supported_devices 300, device-chipset-cve-xref 120,
android-security-bulletin-cves 200, mediatek-cve-chipsets 60.

## What it is NOT

Not a corpus and not a coverage claim. Truncation breaks the joins between
sources -- the 80 GSMArena spec rows mostly describe devices the 200 Samsung
FOTA rows do not mention -- so the device, silicon and firmware counts a batch
over this produces are arbitrary and must never be read as a measurement of the
real sources. It exists to prove that the packaging and ingest PATH works.
