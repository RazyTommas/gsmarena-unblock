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

## The stem rule (2026-09-30)

`identity_backfill.approve_stem_corroborated_identities` is the one rule that
approves a Xiaomi codename the vendor catalog does not carry as a key.

**The stall.** `devices.yml` is keyed by *regional* codename variants —
`vili_global`, `vili_eea_global`, `vili_tw_global` — which is what
`xiaomi.community.firmware_tracker` publishes, and all of them are approved. The
mifirm archive publishes the **bare stem**, `vili`, which is never a catalog key,
so `approve_catalog_confirmed_identities`' exact-key test cannot reach it. 58
registry rows on already-approved products were stuck `proposed` that way, gating
8,332 captured observations that served nothing.

**The corroboration, and the field it is read from.** A stem is approved only when
the product's own recorded conclusion already names it:

```
identity_conclusions.evidence_json
  -> entries whose "source" is "google_play_supported_devices"
  -> their "device_codes" list
```

That list is Google Play's supported-device catalog as captured for that product
(`crawler/relay/results/google-play-devices/supported_devices.csv`), written by
`enrichment._conclude`. Two publishers, one relationship: Google names the device
code `vili`, Xiaomi names `vili_global` with the same marketing name. If the stem
is not in a captured `device_codes` list, the rule refuses — there is nothing else
it would be standing on.

**It refuses:**

| reason | what it catches |
| --- | --- |
| `stem_not_in_captured_device_codes` | no second publisher names the stem |
| `stem_has_no_regional_catalog_variants` | no vendor statement that a stem stands for a family of regional codenames |
| `stem_names_several_products` | Google Play lists the device code for more than one product |
| `stem_registered_on_several_products` | the registry files the value under more than one product |
| `catalog_variants_disagree` | a catalog variant names a different phone once the region suffix is stripped |

**Measured on a copy of the live corpus, 2026-09-30.** 11 of the 58 approved,
releasing **2,080** of the 8,332 observations into `product_firmware_releases`.
Not 8,332: 46 of the stems have no captured Google Play device code for their
product at all, and the 47th is `lisa`, which Google Play lists for two approved
products (Xiaomi 11 Lite 5G NE **and** Mi 11 LE) and whose bare catalog key names
the second one. Two candidates is not a match, so `lisa`'s 361 observations stay
stalled — correctly.

Approval means the captured source identity belongs to the source product, and
nothing else. No hardware model is created, no model code is fabricated, and the
catalog's regional variants are not collapsed.

**`enrichment.RULE_VERSION` is deliberately not bumped for this.** That constant
gates whether a product *conclusion* reopens, and a conclusion reopens only when
it resolved nothing under an older version. Measured: a bump from `"2"` to `"3"`
reopens the 465 conclusions that resolved nothing under version 2, of which **0**
carry a Google Play device code — so the bump would re-decide 465 products to
announce a rule that can resolve none of them. The stem rule versions its own
decisions as `stem-1`, on registry rows whose products are already
`auto_approved` and therefore never reopened.

**Every decision records why.** `identity_resolution_rationales` (migration 0029)
holds one current row per `(identity, rule)` — outcome, machine-readable reason,
prose naming the stem, the catalog variants and the Google Play evidence, plus the
evidence itself. Refusals are recorded as well as approvals: they are the residue
a human has to look at, and re-deriving them from a report that ages out with the
next capture is not the same as having them.

```sql
SELECT reason, count(*) FROM identity_resolution_rationales
 WHERE outcome='refused' GROUP BY 1 ORDER BY 2 DESC;
```

### What the stem rule surfaced and did NOT fix

Measuring the rule end to end exposed a pre-existing field-name mismatch in
`promote_approved_product_observations`, which is **not** the stem rule's to
decide and was deliberately left alone:

- promotion reads `data.release_date`, and the mifirm adapter publishes
  `data.vendor_released_at`;
- promotion reads `data.branch`, and the mifirm adapter publishes `data.channel`.

So **every** mifirm release lands with `vendor_released_at IS NULL` and
`channel='unknown'`: 19,765 of them before the stem rule and 21,845 after. The
consequence a reader can see is `android_version_changed` radar events: their
`before`/`after` pair is ordered by `vendor_released_at, id`, so with every date
NULL the "sequence" falls back to a uuid5 row id. 5,152 of the 5,208 existing
events already rest on at least one undated release; the stem rule adds 596 more
of the same shape and creates none of the problem.

Fixing it would date 21,845 releases, and because promotion is `INSERT OR IGNORE`
and deletes nothing, the hash-ordered events already published would stay
alongside the newly correct ones. That is a decision about retracting published
facts, not a field rename, so it is recorded here rather than taken quietly.

Validate against a real snapshot without modifying the original:

```sh
python3 tools/validate_product_batch.py \
  --source /tmp/mobile-observatory-review-20260916 \
  --output /tmp/mobile-observatory-product-review-20260917
```

The output must be new. Validation backs up corpus and local state, checks exact
preservation of canonical/history/security/conclusion records, enrichment
idempotency, SQLite integrity and every product's complete paginated history.

## The terminal state for what no rule and no reviewer can resolve

Added 2026-09-30. `src/mobile_observatory/adjudication.py` carries the full
argument; this is the contract.

After every rule above has run to its fixed point, **626 of 2,367 products** are
still `review_state='proposed'` — and every one already carries a recorded
conclusion saying the captured evidence cannot settle its identity:

| conclusion | method | products |
|---|---|---:|
| `insufficient_evidence` | `no_independent_identifier` | 465 |
| `ambiguous` | `ranked_candidates` | 82 |
| `ambiguous` | `google_play_model_code_multiple_names` | 63 |
| `ambiguous` | `google_play_name_multiple_models` | 15 |
| `ambiguous` | `google_play_model_code_names_several_devices` | 1 |

`proposed` means "waiting for a reviewer", so those 626 presented **20,955
observation links** as a queue — Xiaomi 14,996, Apple 4,450, TECNO 846, itel 573,
Infinix 90 — that no reviewer could ever clear. Where the corroborating evidence
does not exist, a human brings nothing an agent does not.

They now rest in **`unresolvable_on_captured_evidence`** (migration 0030). A fourth
state, and not one of the three, because:

- `approved` would assert the identity belongs to the product — the exact thing
  that could not be established;
- `rejected` would assert the identity was judged *wrong*, would withdraw the
  product's hardware claim (`server._apply_product_review`) and would block the
  automated rules forever (`enrichment._conclude`'s `blocked`). Nothing was judged
  wrong; it was not judgeable;
- `proposed` names work nobody can do.

The name keeps its qualifier on purpose. "unresolvable" alone is a claim about the
future that no capture supports; `unresolvable_on_captured_evidence` says what was
measured and names the thing that can change.

**Nothing is asserted, promoted or deleted.** `source_identity_registry`,
`observation_product_links`, `identity_conclusions` and every firmware row are
untouched, and the products go on serving from the evidence layer.

**It is reopenable, which is what makes it honest rather than a cover-up.** The
adjudication stores a fingerprint of its basis — the product's registry identities
plus its recorded conclusion. `reopen_stale_adjudications` runs *before* the rules
each batch and returns a product to `proposed`, dropping the frozen conclusion so
the rules decide again, whenever that basis stops describing the corpus. A batch
over unchanged inputs reopens nothing, so the corpus still does not depend on when
it last ran. A human can reopen one from the review inbox using the existing
`proposed` decision; the API deliberately cannot *set* this state, so a human's
"no evidence exists" can never be filed as an agent's.

**The two sub-populations stay two.** "No independent identifier exists" and
"several candidates exist and none discriminates" are different claims about the
world, and only the first means the sources are silent. They are separated by
`identity_resolution_rationales.reason` and reported as separate per-vendor columns
by `integrity.review_queue` (`unresolvable_no_identifier` /
`unresolvable_several_candidates`).

**Provenance is in this same table**, not a second store:
`outcome='adjudicated_unresolvable'`, `rule='unresolvable_on_captured_evidence'`
with its version, and `evidence_json.decided_by='agent:unresolvable_adjudication'`
— none of which any other writer uses, so an agent adjudication stays
distinguishable from a human decision (`resolution_method='manual_product_review'`,
history in `local.sqlite`) and from a source having proved the identity
(`outcome='approved'` under a named rule). Migration 0030 makes `identity_id`
nullable for the **2** products (Xiaomi "Redmi 1", itel "ACE2N") that carry no
registry identity to key a decision on; they are counted as
`unresolvable_without_identity` rather than exempted silently, and `check_corpus`
raises `adjudicated_product_without_recorded_basis` (error) if any product rests in
the state with no recorded basis.

**The queue metric stops counting them without hiding them.**
`observations_awaiting_review` is now `review_state='proposed'` — what the UI
tooltip always claimed it was, and which a *rejected* product's observations used
to fail — and the adjudicated population is reported beside it as
`observations_adjudicated_unresolvable`. `observations_not_serving` is unchanged:
they still do not serve. `write_agent_review_bundle` likewise narrows to
`review_state='proposed'`, so the handoff stops offering an agent 626 questions the
corpus has recorded as unanswerable.

```sh
python3 -m mobile_observatory.integrity --data-dir .observatory-data
```
