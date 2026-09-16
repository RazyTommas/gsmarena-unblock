# Captured LineageOS specification evidence

The 2026-09-17 capture contains 680 phone, tablet and foldable source targets
from the official LineageOS wiki repository at commit
`592d086c0603c6337c3733d0f11f3f0d018be794`. The immutable raw YAML source URL,
full file, SHA-256, observation timestamp, manufacturer, exact target name,
codename, chipset string and explicit model list are preserved for every row.
This improves on the old 577-row CSV, which omitted raw YAML/commit provenance.

The data comes from the LineageOS community, not hardware manufacturers. It is
medium-confidence specification evidence. LineageOS branches, custom-ROM support,
stock-firmware prerequisites and download/install instructions are deliberately
not imported as stock Android versions, OEM support, ROM packages or security
applicability. Existing hardware identities and hardware-silicon mappings do not
change. A captured model list can provide separate exact-model evidence for an
existing hardware record; `/DS` and regional model variants are never inferred.

Existing approved products are reused only through a unique exact codename or
unique exact manufacturer/product name. Variant qualifiers and `+` are retained.
Multiple candidate products, blocked review decisions and differing chipset
strings remain explicit unlinked source assertions. New standalone targets use
source-scoped identities, so two wiki variants sharing a codename cannot merge.
Original specification evidence is not replaced, including when chip spellings
differ. Google Tensor, NVIDIA Tegra, Intel Atom and TI OMAP vendor strings are now
recognized without fabricating exact part numbers or revisions.

Apply to a copied runtime (standard-library only):

```sh
PYTHONPATH=src python3 -m mobile_observatory.lineage_specs \
  --data-dir /path/to/copied-runtime \
  --capture source-data/lineageos-2026-09-17/capture.json
```

The importer reads remembered local identity decisions, validates every hash and
source URL before writing, copies evidence beside the runtime database, and is
idempotent. `hardware_specification_evidence(connection, model)` returns separate
source assertions with `canonical_mapping: false` for an exact model code.

To produce a new capture, install PyYAML in a capture-only environment and run
`tools/capture_lineage_specs.py --commit FULL_UPSTREAM_SHA --output DIRECTORY`.
This reads GitHub with six bounded workers and HTTP timeouts; it does not modify
the application database. Inspect changes and validate a copy before importing.

## Attribution and scope

Source: [LineageOS wiki repository](https://github.com/LineageOS/lineage_wiki).
Copyright LineageOS contributors. Wiki content is distributed under
[CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/), as declared in
the preserved `LICENSE-footer.html`. The repository's `licenses/LICENSE` is the
MIT license for the site template and is preserved separately as `LICENSE`; it
is not used to relabel wiki content. The normalized capture is an adaptation
(selected fields and mobile form-factor filtering), distributed under CC BY-SA
3.0 with the source files unchanged. No image or firmware binaries are copied.
