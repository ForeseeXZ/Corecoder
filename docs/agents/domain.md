# Domain docs

CoreCoder is a single-context repository. Engineering skills should use the following sources before exploring or changing a domain area.

## Sources

- Read the root `CONTEXT.md` for the shared language and relationships.
- Read relevant decisions under `docs/adr/` when that directory and matching ADRs exist.
- If a referenced document does not exist, continue without treating its absence as an error.

## Vocabulary

Use the terms defined in `CONTEXT.md` in Issue titles, test names, implementation plans, and reports. Do not replace them with synonyms that the glossary explicitly marks as ambiguous or discouraged.

If a needed concept is missing, first check whether an existing domain term already covers it. Record a genuine terminology or architectural decision as an ADR only when the decision is actually made.

## ADR conflicts

If proposed work contradicts an existing ADR, identify the ADR and surface the conflict instead of silently overriding the recorded decision.
