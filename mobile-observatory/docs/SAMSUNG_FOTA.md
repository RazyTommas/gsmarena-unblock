# Samsung FOTA lane

Samsung's OTA manifest is retained as an authoritative observation of firmware availability. It exposes a triplet: AP/PDA build, CSC build, and CP/baseband build. These are stored as separate facts; CP must never be derived from AP.

The production boundary is deliberately split:

1. A scheduled capture runner requests metadata only from `fota-cloud-dn.ospserver.net` for a validated model/CSC inventory. Existing host-budget and known-control safeguards remain required.
2. `SamsungFotaArtifactAdapter` parses captured XML without network access and emits staging observations.
3. The ingestion importer preserves the raw artifact and observation provenance.
4. `SamsungFirmwarePromoter` promotes only a unique, exact Samsung model-code match. Unknown or ambiguous identities remain staged.
5. A promoted new build appends a deduplicated change event. Replaying the same capture creates neither duplicate releases nor events.

No firmware binary, credential, fuzzy match, or direct collector write to canonical tables is allowed. The four initial reviewed profiles—flagship, foldable, midrange, and entry—are in `fixtures/samsung/reviewed_profiles.json`; their builds are explicitly labeled as captured corpus history rather than current upstream truth.
