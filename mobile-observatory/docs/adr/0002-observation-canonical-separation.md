# ADR 0002: Separate observations from canonical facts

Status: accepted

Collectors write immutable artifacts and staged observations. Only the resolver
and promotion workflow write canonical facts, with evidence. This preserves
contradictions, enables parser replay, and contains collector failures.
