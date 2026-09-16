# ADR 0004: Preserve typed identity hierarchies

Status: accepted

Device family, sellable variant, hardware model, and firmware target are separate
entities. Silicon vendor, family, exact part, and revision are separate entities.
This costs additional joins but prevents false equivalence, enables exact filters,
and preserves regional/hardware distinctions. Convenience comes from versioned
read views, never denormalizing identity into ambiguous strings.
