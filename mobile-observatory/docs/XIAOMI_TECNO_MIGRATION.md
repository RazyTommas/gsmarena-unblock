# Xiaomi and TECNO source migration

This batch ports two already-captured sources into the observation pipeline. It
does not bulk-promote their strings into canonical devices.

## Xiaomi firmware tracker

`XiaomiFirmwareTrackerAdapter` ingests the captured latest-release CSV as
`firmware_release` observations. Each row retains the source codename, display
name, Android version, build, branch, release date and derived source region.

The source is community-maintained. A codename such as `songyuan_global` is an
identity hint, not an official hardware model code. The resolver must find an
accepted, durable mapping before these observations can populate a canonical
device's firmware history. One codename can also describe several marketed
devices, as the `arctic` records demonstrate.

## TECNO security device scope

`TecnoSecurityPatchAdapter` ingests the official TECNO device-scope capture as
`security_patch_publication` observations. Multi-device cells are split while
the original group is retained as evidence. The source supplies ASPL month
precision only; the adapter never fabricates a day.

TECNO display names remain unresolved until model-code evidence distinguishes
hardware and regional variants. Consequently, a publication is not yet a
device-level security verdict. It is staged evidence available to the resolver.
The feed covers TECNO only, not Infinix or itel.

## Review profiles

`tools/build_xiaomi_tecno_profiles.py` creates eight review-only profiles in
`fixtures/xiaomi_tecno_review_profiles.json`:

- Xiaomi: POCO F9 Ultra, POCO F9 Pro, Redmi Note 17 Pro Max, Redmi A7 Pro.
- TECNO: PHANTOM V Fold2 5G, CAMON 40 Pro 5G, POVA 7 5G, SPARK 40C.

Xiaomi profiles combine captured GSMArena specifications and firmware targets.
TECNO profiles contain the full available patch-publication history and clearly
label missing firmware, Android, chipset, and hardware-model coverage. These
gaps must stay visible rather than be inferred.

## Running the adapters

Adapters are deterministic over immutable captured artifacts and can be passed
to `CollectorPipeline`. Production fetching/scheduling remains a separate step;
the checked-in source captures make parser behavior replayable in tests.
# Product histories and Android evidence

The replay now consumes the captured Xiaomi `latest.yml` archive, not only its
latest-per-target CSV. It yields 3,595 immutable source observations. Approved
codename-to-product identities are projected into `product_firmware_releases`;
they are deliberately not inserted into canonical `hardware_models` unless an
authoritative hardware model code exists.

TECNO's official device-scope rows sometimes contain slash- or comma-separated
product lists. The adapter emits one observation per named product and retains
the original group as evidence. Approved records are projected into
`product_security_publications`, preserving the vendor's month-only SPL
precision.

Android upgrade events require increasing Android majors in chronologically
ordered releases for the same approved product, region and channel. The system
does not label cross-region differences or apparent downgrades as upgrades.

Read APIs:

- `GET /api/v1/product-releases` supports `q`, `maker`, `product`, `region`,
  `channel`, `limit`, and `cursor`.
- `GET /api/v1/product-security` supports `q`, `limit`, and `cursor`.

The captured run resolves 564 of 566 commercial products. Two early Redmi 1
names remain explicitly deferred because the catalog does not prove whether
they are separate variants. Google Play model-code candidates for TECNO remain
visible as variant candidates and are never collapsed into one invented code.
