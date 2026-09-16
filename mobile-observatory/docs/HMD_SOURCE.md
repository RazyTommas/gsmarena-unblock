# HMD vendor security and maintenance statements

Source: [HMD Security and Maintenance Updates](https://www.hmd.com/en_int/security-updates).
The existing preserved CSV was captured at `2026-09-13T11:04:49Z`; its metadata
and 2,872 factual rows remain under `crawler/relay/results/hmd-security/`.
The live source page was checked again during this batch and identifies its last
update as September 7, 2026. Importing a capture does not imply refreshing it.

HMD explicitly distinguishes the first release to approved markets from when all
users receive an update. Accordingly, `dateOfFirstLiveRelease` is retained as the
first-live-release date, while market/region remains `SOURCE_UNSPECIFIED`.
`androidBulletinDate` is a distinct vendor patch statement with its original day
precision. It is not publication time and does not establish applicability or
fixes for individual CVEs. No publication date is fabricated.

`screenId` is a firmware build variant, never a device model or codename. Product
names are linked by unique exact name and maker only. Explicit numeric Android
versions in vendor comments are retained; Android marketing names alone are not
converted to numeric releases. Vague estimated cadence/end-of-life labels remain
in source observations and do not become canonical support assertions. No ROM
download URL, region or chipset is invented.

The raw CSV is preserved beside the runtime corpus with a SHA-256. Every parsed
row retains its original fields, source URL, observation timestamp, CSV locator
and immutable evidence. This import yields 2,871 distinct firmware and patch
statements across 87 products: one source row is an exact duplicate. On the
LineageOS-enriched corpus, six exact Nokia names are reused and 81 products are
new. Canonical hardware, silicon mappings, support and CVE claims stay unchanged.
Unknown-market statements do not produce Android-upgrade events, which would
incorrectly compare potentially different regional/hardware rollouts.

```sh
PYTHONPATH=src python3 -m mobile_observatory.hmd_updates \
  --data-dir COPIED_RUNTIME \
  --capture ../crawler/relay/results/hmd-security/hmd-security-updates.csv \
  --observed-at 2026-09-13T11:04:49Z
```

The import reads local remembered identity decisions and is idempotent. No
third-party Python dependencies are required. Captured factual metadata is kept
for offline evidence; vendor copyright and trademarks remain with their owners.
No site design, images, manuals or firmware binaries are redistributed.

Product Evidence now uses maker-neutral firmware and security tabs. Select HMD
or Nokia, then open a product to inspect build history, literal Android comments,
vendor patch statements and the first-approved-market qualification. Absence of
a package link is explicit source absence, not an invented download action.
