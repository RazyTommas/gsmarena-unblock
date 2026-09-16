# Automated identity enrichment

The enrichment pass concludes every staged Xiaomi and TECNO product, but only
approves deterministic relationships supported by captured independent evidence.

- Xiaomi codenames are checked against the captured `devices.yml` vendor catalog.
- Commercial names are compared by exact normalized equality with captured GSMArena
  specifications and the Google Play supported-device catalog.
- Multiple Google Play model codes remain ambiguous; they are not collapsed.
- Missing evidence is recorded as `insufficient_evidence`, never interpreted as a
  negative or as permission to invent a hardware identifier.
- Exact chipset assertions are retained in `observed_product_silicon`; they do not
  become canonical hardware assignments until identity is resolved.
- Existing canonical hardware receives silicon only through exact model-code joins
  with the captured cross-reference dataset.

Every unresolved conclusion is exported as `agent-review-prompt.md` plus
`identity-candidates.json`. The prompt tells a local agent the product vision,
evidence hierarchy and expected JSON response. Agent output remains a proposal and
must pass the same validation/promotion boundary as manual decisions.

Captured Android and MediaTek bulletin rows populate advisories and vulnerabilities.
They deliberately do not claim that a device is affected merely because a CVE exists;
applicability requires explicit chip/component evidence.

## Product specification expansion (2026-09-17)

`product_specs.enrich_product_specs` preserves vendor boundaries, `+`, network and
regional qualifiers. It matches exact commercial names, aliases from an approved
exact catalog codename, or names from an unambiguous Google Play identifier.
Google Play models are corroboration only; they never supply chipset assertions.
Several specification candidates or conflicting rows do not merge. Existing
identity conclusions, manual review states and negative/deferred local decisions
are retained by replay enrichment.

A unique captured specification with no firmware product becomes a standalone
product-level record. This does not invent ROM history, support status, Android
upgrade state, security applicability or hardware model codes. Launch OS remains
specification evidence. Missing chipset rows are not imported as silicon links.

Every silicon observation keeps its own GSMArena evidence (fixing the previous
last-evidence-entry selection), URL, row locator, observation time, complete
specification and SHA-256. File-backed imports preserve input bytes under the
snapshot's `evidence/` directory. Existing tables suffice; no canonical schema or
relationship semantics changed.

Validate against a real snapshot without modifying the original:

```sh
python3 tools/validate_product_batch.py \
  --source /tmp/mobile-observatory-review-20260916 \
  --output /tmp/mobile-observatory-product-review-20260917
```

The output must be new. Validation backs up corpus and local state, checks exact
preservation of canonical/history/security/conclusion records, enrichment
idempotency, SQLite integrity and every product's complete paginated history.
