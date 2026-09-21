# Google AOSP source-build evidence

The public [AOSP build reference](https://source.android.com/docs/setup/reference/build-numbers)
was captured as original HTML with SHA-256 and an observation timestamp. Its
1,032 explicit build/tag rows are stored separately from firmware releases.
There are 1,522 exact Pixel product associations; blank product lists remain
unassigned. In particular, Android 16/17 catalog rows do **not** identify supported
Pixel products, so this capture does not establish those device/Android mappings.

Source tags explicitly name Android versions. The security-patch column is kept
separate from release dates; no release or publication date is synthesized from
a build ID, source tag, patch date or capture time. A source-build device list
does not prove an OTA rollout, device security applicability or current OEM
support. No ROM download URL is generated and no firmware package is downloaded.
The public OTA landing page was inspected but its catalog is behind a terms
acknowledgement; no agreement was submitted and no hidden package URLs invented.

Product detail has a separate, collapsed **AOSP source-build evidence** section
with paging, tag, literal version, patch statement and captured source proof.
The source's explicit Pixel lists end in May 2025 in this capture. This is limited
historical source-build coverage; it must not be read as saying a Pixel is stuck
on that Android version today. Other brands/Nexus rows remain queryable in the
source catalog but are not assigned a guessed manufacturer.

Apply to a copied runtime:

```sh
PYTHONPATH=src python3 -m mobile_observatory.google_builds \
  --data-dir COPIED_RUNTIME \
  --capture source-data/google-builds-2026-09-17/capture.json
```

Migration 0015 adds only source build and product association tables. The importer
validates source URL, file containment, complete source hash, table headers and
date formats before database changes; standard library only. Local rejected or
deferred identities are respected. Canonical identities, silicon, firmware,
support assertions and security applicability remain unchanged.

Attribution: The Android Open Source Project. The captured page declares
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) for documentation;
its source HTML retains the declaration. Parsed factual tables are an adaptation
with exact product-name association, not edits to the source text. Software
binaries have separate terms and are not included in this capture.
