## SQ2 — Correctness of the answers to the information needs FR4–FR15 (precision / recall against CSV ground truth)

Prototype v3.1. Preset values are passed as parameters (no values fixed in the query text).


### State S0 — after import

| FR | Answered by | |GT| | csv | xlsx | mspdi |
|---|---|---|---|---|---|
| FR4 | q01 | 3 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR5 | q05 | 0 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR6 | q06 | 16 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR7 | q07 | 23 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR8 | q08 | 1 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR9 | q10 | 5 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR10 | q13 | 25 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR11 | q18 | 1 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR12 | q16 | 38 | 1.00 / 1.00 | 1.00 / 1.00 | 0.00 / 0.00 |
| FR13 | API /progress | 15 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR14 | API /impact | 10 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR15 | API /delays | 12 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |

### State S1 — after the scripted status updates

| FR | Answered by | |GT| | csv | xlsx | mspdi |
|---|---|---|---|---|---|
| FR4 | q01 | 3 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR5 | q05 | 1 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR6 | q06 | 13 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR7 | q07 | 23 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR8 | q08 | 1 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR9 | q10 | 4 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR10 | q13 | 25 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR11 | q18 | 3 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR12 | q16 | 38 | 1.00 / 1.00 | 1.00 / 1.00 | 0.00 / 0.00 |
| FR13 | API /progress | 15 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR14 | API /impact | 10 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR15 | API /delays | 11 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |

**Fully correct answers (P = R = 1) out of 24 (12 requirements × 2 states):** csv: 24, xlsx: 24, mspdi: 22

### Parameter sweep (S0) — parameterised presets answered for every value, fully correct / cases

| FR | Preset | Parameter range | csv | xlsx | mspdi |
|---|---|---|---|---|---|
| FR4 | q01 | every actor | 14/14 | 14/14 | 14/14 |
| FR7 | q07 | every process | 38/38 | 38/38 | 38/38 |
| FR8 | q08 | every process | 38/38 | 38/38 | 38/38 |
| FR9 | q10 | every month end | 10/10 | 10/10 | 10/10 |
| FR10 | q13 | every month | 10/10 | 10/10 | 10/10 |
| FR11 | q18 | every process | 38/38 | 38/38 | 38/38 |

### FR16 — versioned status change (S1)

| Check | csv | xlsx | mspdi |
|---|---|---|---|
| one state per change | pass | pass | pass |
| exactly one current state | pass | pass | pass |
| old states invalidated | pass | pass | pass |
| revision chain in RDF | pass | pass | pass |
| effective dates recorded | pass | pass | pass |
| actual start = date of start | pass | pass | pass |

### FR17 — re-plan of the FR14 delay (after S1)

| Check | csv | xlsx | mspdi |
|---|---|---|---|
| schedule states written = GT | pass | pass | pass |
| new finish dates = GT | pass | pass | pass |
| started keep start, others move | pass | pass | pass |
| other processes unchanged | pass | pass | pass |
| baseline still queryable (q19) | pass | pass | pass |
| ABox conforms after write | pass | pass | pass |

### FR14 detail (CSV, S0): '1st Fix Plumbing — Pipework & Waste' 21 days late

- affected successors: 9, absorbed by float: 0, actors to inform: 7, project end: 2025-12-22 → 2025-12-26 (+4 days)
- delayed process (started): start 2025-09-08 → 2025-09-08 (kept), finish 2025-09-19 → 2025-10-10
- shifts: Internal Blockwork Partitions +21 d, Thermal & Acoustic Insulation — Floors & Partitions +21 d, Plasterboard Dry Lining & Ceilings +19 d (2 d absorbed by float), Wet Plaster Skim & Finish +17 d (2 d absorbed by float), 2nd Fix Electrical — Faceplates & Testing +15 d (2 d absorbed by float), 2nd Fix Plumbing — Sanitaryware & Commissioning +15 d (2 d absorbed by float), Decoration — Internal Paint & Finishes +1 d (16 d absorbed by float), Kitchen & Bathroom Fit-Out +13 d (2 d absorbed by float), Building Control Inspection & Sign-Off +4 d (9 d absorbed by float)

### FR15 detail (CSV, S0): reference date 2025-10-01

- late processes: 6 (External Landscaping — Phase 1 Enabling +16 d, Thermal & Acoustic Insulation — Floors & Partitions +16 d, Internal Blockwork Partitions +16 d, 1st Fix Plumbing — Pipework & Waste +12 d, External Render & Insulated Cladding +5 d, 1st Fix Electrical — Containment & Wiring +5 d); knock-on: 5 successors, 5 actors, project end 2025-12-22 → 2025-12-22 (+0 days)