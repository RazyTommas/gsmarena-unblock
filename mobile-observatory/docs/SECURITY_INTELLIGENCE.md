# Security and silicon evidence

The security model distinguishes a bulletin mentioning a CVE from evidence that a
specific component, silicon part, device, or firmware is affected.

Captured Android Security Bulletin rows create advisory membership, named-component
applicability claims, and `android_spl` fix coordinates only when the bulletin row has
an explicit `01`, `05`, or `06` patch tier. These facts do not by themselves prove that
an individual handset contains the component or received the fixing patch.

Captured MediaTek bulletin rows create `affected` applicability claims only for exact
`MT####`/`MT#####` identifiers enumerated in the vendor row. Device counts are derived
only through independently stored `hardware_silicon` assignments. Every claim points
to row-level evidence in an immutable captured artifact.

There is no captured authoritative Qualcomm advisory-to-part mapping in this repository.
Android bulletins do contain Qualcomm component sections, but converting those sections
to Snapdragon part applicability would be an unsupported inference. The database records
this as a `security_coverage_gaps` entry and exposes it at
`GET /api/v1/security/coverage`.

Read models:

- `v_security_findings`: advisory, applicability, derived device, and fix counts by CVE.
- `v_silicon_security`: device, affected-CVE, and fix-coordinate counts by silicon part.
- `GET /api/v1/security/findings`: paginated API projection with evidence state.
- `GET /api/v1/chips`: silicon inventory with affected-CVE and unresolved-fix counts.

An SPL coordinate means the bulletin claims the issue is addressed at that Android SPL.
It is not a verdict for a device. A device verdict requires both supported applicability
and firmware evidence proving that the device runs a qualifying patch level.
