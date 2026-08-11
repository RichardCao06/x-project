# Wiki Phase 2 defect corpus

Each file is a minimal, immutable replay input for a historical Wiki safety
failure.  Tests must consume these files as inputs to a gate, release
transaction, or domain adapter; they are deliberately not configuration-only
fixtures.

| Fixture | Failure class | Expected protection |
| --- | --- | --- |
| `source-not-claim.json` | Source trust confused with claim verification | G4 / reviewed gate |
| `adjacent-evidence.json` | Adjacent object promoted to target evidence | G4 |
| `identity-swap.json` | Frozen node join changed in agent result | G1 |
| `old-gate.json` | PASS from another candidate reused | G7 |
| `coverage-drop.json` | Unresolved claims silently removed from denominator | coverage gate |
| `unsafe-urls.json` | local/private URL accepted as external evidence | fetch protocol |
| `shared-footnote.md` | sentence-level claims disappear behind a shared footnote | coverage extractor |
| `generic-gap-shell.md` | ten-section evidence-gap shell | draft content gate |
| `product-activity-confusion.md` | Product page contains activity-only material | type contract |
| `golden-regression.md` | candidate loses Golden content | non-degradation gate |
| `reordered-preview.html` | renderer changes chapter order | preview gate |
