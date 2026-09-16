# ADR 0005: Security verdict vocabulary is explicit

Status: accepted

Verdicts use open, claimed fixed, unknown, not adjudicable, or not applicable.
Every verdict records a versioned rule, input snapshot, subject, evaluation time,
and evidence. Android SPL, component versions, firmware builds, and vendor releases
are distinct fix-coordinate types and cannot be compared without a rule that knows
their vocabulary.
