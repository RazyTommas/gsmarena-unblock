# Product specification

## Product promise

Mobile Observatory helps one technically sophisticated user answer four
questions quickly and with inspectable evidence:

1. Which watched, currently-supported phones received firmware since the last
   visit, and what changed?
2. Which devices contain a silicon vendor, family, exact part, or revision?
3. How does an advisory or CVE relate to a component, chip, device variant,
   firmware build, and security patch?
4. Which supported devices are on, above, or below a selected Android version?

The system prioritizes global releases and Israel / Middle East / Levant market
coverage. Collection is expected several times per day. Dashboard delivery is
the initial notification channel.

## Applications

### Radar

An inbox of immutable change events scoped by watches. A firmware card shows
device and hardware identity, market/firmware target, old and new build,
Android change, security-patch change, baseband change, release time, first
observation time, source freshness, and evidence. Seen state is user-local and
does not mutate the event.

### Explorer

Alias-aware search and indexed facets for brand, family, hardware model,
market, support state, Android state, silicon vendor/family/part/revision, and
data completeness. Empty results distinguish no matching facts from missing or
stale coverage.

### Security

Table-first traversal of advisory/CVE -> affected component -> silicon ->
hardware -> firmware/fix evidence. A graph is secondary. Every verdict exposes
the rule, inputs, evidence, and evaluation time.

### Admin

Source freshness, run outcomes, inventory contraction, unresolved identities,
quarantined observations, proposal review, coverage gaps, and snapshot export.
Operational metrics may also be projected into Grafana; domain exploration may
not depend on Grafana.

## MVP scope and acceptance criteria

- Govern currently supported Samsung first, then Xiaomi/Redmi/Poco and Pixel.
- Find a device by commercial name, model code, codename, or reviewed alias.
- Filter supported devices by exact silicon part and latest observed Android.
- Presets: Israel, Middle East/Levant, Global, Europe/EEA, all markets.
- Common local queries complete in 200 ms on the intended corpus; first useful
  local render in one second.
- New firmware is visible after the next successful source run (target: every
  six hours), with source failure displayed separately.
- A portable snapshot contains canonical SQLite, local user state separately,
  cached evidence when permitted, manifest, schema version, hashes, and export
  formats. Snapshot age is always visible.
- Missing information is never presented as a negative result or safety.

## Non-goals for the MVP

- Historical-device completeness.
- Push, email, or chat notifications.
- Autonomous agent writes to canonical facts.
- Compatibility with the legacy UI, API, or database.
- One universal security-patch comparison for all advisory types.
