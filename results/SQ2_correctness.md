## SQ2 — Correctness of the answers to the information needs FR4–FR15 (precision / recall against CSV ground truth)


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
| FR13 | q04 | 14 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
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
| FR13 | q04 | 14 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR14 | API /impact | 10 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| FR15 | API /delays | 11 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |

**Fully correct answers (P = R = 1) out of 24 (12 requirements × 2 states):** csv: 24, xlsx: 24, mspdi: 22

### FR14 detail (CSV, S0): '1st Fix Plumbing — Pipework & Waste' 21 days late

- affected successors: 9, absorbed by float: 0, actors to inform: 7, project end: 2025-12-22 → 2025-12-26 (+4 days)

### FR15 detail (CSV, S0): reference date 2025-10-01

- late processes: 6 (Internal Blockwork Partitions +16 d, 1st Fix Plumbing — Pipework & Waste +12 d, External Render & Insulated Cladding +5 d, External Landscaping — Phase 1 Enabling +5 d, Thermal & Acoustic Insulation — Floors & Partitions +5 d, 1st Fix Electrical — Containment & Wiring +5 d); knock-on: 5 successors, 5 actors, project end +0 days