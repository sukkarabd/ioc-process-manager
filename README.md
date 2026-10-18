# IoC Process Manager

Prototype developed for the Master's thesis of Abdullah Souccar (M.Sc. Construction & Robotics,
RWTH Aachen University, Chair of Individualized Production, 2026).

The application unifies construction scheduling data from CSV files, Excel workbooks, and
MS Project XML files as instance data (ABox) of the Internet of Construction Process Ontology
(IoC v0.5.1, TBox), propagates the impact of delays along the process dependencies, and makes
the result accessible through a graphical user interface (humans) and a REST/SPARQL API
(machines), without requiring users to write SPARQL queries or edit RDF data.
The IoC ontology is loaded read-only from `ioc_tbox.ttl`.

## Run

    pip install -r requirements.txt
    uvicorn server_fastapi:app --port 8000
    # open http://localhost:8000

## Contents

| Path | Purpose |
|---|---|
| `server_fastapi.py` | Backend: format adapters, IoC mapping, delay-impact propagation, validation, REST API |
| `index.html` | Frontend (React 18, single file) |
| `ioc_tbox.ttl` | IoC v0.5.1, converted without change from the official `ioc.owl` (OWL functional syntax) at github.com/Internet-of-Construction/IoC-Process-Ontology |
| `sample_data/` | Thornfield Court test scenario as CSV, Excel workbook, and MS Project XML |
| `tools/make_mspdi_fixture.py` | Regenerates the MS Project XML file from the Excel workbook |
| `tools/evaluate_unification.py` | Evaluation of SQ1 (integration, FR1–FR3): coverage, preservation, TBox conformance, cross-format agreement |
| `tools/evaluate_tracking.py` | Evaluation of SQ2 (tracking and delay impact, FR4–FR17): answers against a ground truth computed directly from the CSV, before (S0) and after (S1) scripted status updates; parameter sweep over all actors/processes/months; FR16 and FR17 checks |
| `results/` | Output of the SQ1 and SQ2 evaluation, and the SQ3 walkthrough protocol (filled in by hand) |

## Parameterised questions (v3.1)

The SPARQL presets no longer contain fixed actors, processes or dates. Each preset declares its
parameters (actor, process, date, phase); the GUI shows them as drop-down lists filled from the
open project, and the server inserts the chosen value into the query. The same presets are
available to machines, e.g.

    GET  /api/projects/{pid}/presets/q07?process=P15
    POST /api/projects/{pid}/presets/q13/run   {"params": {"from": "2025-09-01", "to": "2025-11-01"}}

The SPARQL text stays visible and editable for users who want to write their own query.

## Requirement labels

The SPARQL presets, the evaluation scripts, and the results use the requirement labels of the
thesis: FR1–FR3 (integration), FR4–FR15 (information needs, incl. delay impact FR14/FR15),
FR16 (versioned status change), FR17 (re-plan), and NFR1–NFR3.

## Thornfield Court test scenario

Thornfield Court is a **synthetic** scenario constructed for the thesis. It describes a fictitious
four-storey residential building with 24 apartments; all companies, persons, and dates are invented.
It is not taken from a real construction project.

- 38 processes in four phases (March–December 2025)
- 51 dependency links, 6 of which overlap (successor starts before predecessor ends)
- 14 actors from 14 companies, 16 locations
- Reference state: 25 finished, 3 started, 10 planned processes

The CSV file and the Excel workbook have identical content; the workbook additionally contains the
phase structure. The MS Project XML file is generated from the workbook with
`tools/make_mspdi_fixture.py` and, in line with the format, contains no methods, locations, or
output elements.

## Reproduce the evaluation

    F="sample_data/thornfield_court_sample_data.csv sample_data/thornfield_court_sample_data.xlsx sample_data/thornfield_court.xml"
    python tools/evaluate_unification.py $F     # SQ1 -> results/SQ1_integration.*
    python tools/evaluate_tracking.py $F        # SQ2 -> results/SQ2_correctness.*

The first file is the reference format. Answering routes and interaction steps (SQ3) cannot be
measured automatically; they are recorded in `results/SQ3_walkthrough_protocol.csv`.

## Main endpoints

| Method | Path | |
|---|---|---|
| POST | `/api/import/file` | `{name, filename, data(base64)}` — CSV / XLSX / MSPDI, returns import report |
| GET | `/api/projects/{pid}/processes[/{id}]` | process data (current states) |
| POST | `/api/projects/{pid}/processes/{id}/status` | `{status, progress?, by?, date?}` → new versioned status state (FR16); `date` = when it happened → actual start/finish |
| GET | `/api/projects/{pid}/processes/{id}/history` | all status and schedule states + baseline / plan / actual comparison (FR11) |
| GET | `/api/projects/{pid}/progress?date=` | completion overall, per phase, per actor (FR13) and completion date baseline / plan / forecast |
| POST | `/api/projects/{pid}/impact` | `{process, delayDays \| newEndDate}` → what-if delay impact, read-only (FR14) |
| POST | `/api/projects/{pid}/impact/apply` | same body + `by` → re-plan: new schedule states, baseline kept (FR17) |
| GET | `/api/projects/{pid}/delays?date=YYYY-MM-DD` | late processes at the reference date and their combined knock-on effect (FR15) |
| POST | `/api/projects/{pid}/delays/apply?date=YYYY-MM-DD` | record that forecast as a re-plan (baseline kept) |
| GET | `/api/projects/{pid}/validate` | check of the ABox against the TBox, rules R1–R7 (FR3) |
| GET | `/api/projects/{pid}/ontology` · `/api/tbox` | TBox-derived class/property description |
| POST | `/api/projects/{pid}/sparql` | read-only SPARQL |
| GET | `/api/sparql/presets` | 21 preset queries with their parameters, tagged with the requirements FR4–FR15 |
| GET | `/api/projects/{pid}/sparql/options` | values for the parameter drop-downs (actors, processes, phases) |
| POST/GET | `/api/projects/{pid}/presets/{qid}[/run]` | run a preset with parameter values |

## Delay propagation — assumptions

IoC relates processes through `ioc:hasPredecessor` without link type or lag. The
propagation therefore assumes finish-to-start links and calendar days and never moves
finished or cancelled processes. A process that has not started keeps its duration (start
and finish move together); a process that has already started keeps its start and only its
finish moves. It propagates shifts
through the float of each link: `shift(s) = max_p max(0, shift(p) − gap(p→s))`,
`gap = max(0, start(s) − end(p) − 1 day)`. Where the baseline already overlaps
(6 of 51 links in Thornfield Court), the link has no float and a delay is passed on
in full. SPARQL 1.1 can find the affected successors (`ioc:hasPredecessor+`) but not
compute this longest-path recursion, so it runs in the application layer.

## Planned vs actual

IoC 0.5.1 has one kind of schedule: `prov:startedAtTime` / `prov:endedAtTime` on `ioc:Schedule`
(the plan). It does not distinguish planned from actual dates. The prototype therefore keeps
* the **baseline** = the first `ioc:Schedule` state (kept when a re-plan writes a new one),
* the **current plan** = the current `ioc:Schedule` state,
* the **actual** start / finish as `ext:actualStart` / `ext:actualEnd` on the status value, set
  from the date entered with a status change (`ext:effectiveDate`; the time of recording stays
  in `prov:generatedAtTime`). MS Project `ActualStart` / `ActualFinish` are imported.

All of it is part of the ABox and therefore of the Turtle export.

## Changes in v3.1.1

Fix found in the walkthrough on v3.1.0: after "Record as new plan" or a status change, the process
panel and the Process Board reloaded and the confirmation message disappeared; they now stay
visible while the data reload (frontend only; SQ1/SQ2 results unchanged).

## Changes in v3.1 (formative iteration after the walkthrough)

1. Presets parameterised (actor / process / date / phase as drop-downs; no fixed values in the
   query text); q07 shows direct vs indirect successors; machine access with parameters.
2. Re-plan: a started process keeps its start, only its finish moves (before, the start moved too).
3. "Late" at a status date: a process that has not started is late by (status date − planned
   start); before, a not-started process whose planned end had passed got only
   (status date − planned end), which under-estimated the delay.
4. Delay impact and Delay Watch show planned → new finish per process, the predecessor that
   drives the shift and the days absorbed by float; one summary sentence; details collapsed.
5. Delay Watch forecast can be recorded as a re-plan; project completion shown as baseline /
   current plan / forecast on the Process Board; board column "vs baseline".
6. Planned vs actual dates (FR11): effective date of status changes, actual start / finish,
   comparison table in the history.
7. FR13 answered on the Process Board (progress per phase and per actor), no SPARQL needed.
8. Evaluation: presets called with parameters; parameter sweep; FR16 / FR17 checks.

## Changes compared with v1

1. TBox loaded at run time; ontology browser and validation derived from it.
2. Status **and schedule** versioning per the IoC ProcessDataState pattern (old state retyped to `ioc:ProcessDataState`, `prov:invalidatedAtTime`, `prov:wasRevisionOf`, optional `ioc:generatedByActor`). Previously overwritten.
3. Delay impact: what-if, re-plan, Delay Watch view.
4. Namespaces: ABox individuals no longer minted in `ioc:`; non-IoC terms moved to `ext:` (`urn:iocpm:ext#`).
5. Schedule times as `xsd:dateTime` (IoC range) instead of `xsd:date`.
6. XLSX (phase rows → `ioc:hasParent`) and MS Project XML adapters; import report.
7. List fields split on `;` and `,`; SPARQL presets rewritten and tagged FR4–FR15.
8. Input validation (unknown status 400, duplicate IDs 409, unknown predecessors 400); SPARQL endpoint read-only; delete removes orphan state nodes.
9. Frontend: file import for three formats, history, delay impact, Delay Watch, Gantt baseline markers, TBox-driven ontology browser, validation panel.

Projects created with v1 still open, but fail validation (≈457 violations for Thornfield); re-import them.

## Licence

Code: MIT (see `LICENSE`). Test data in `sample_data/`: CC BY 4.0.
`ioc_tbox.ttl` remains under the licence of the IoC Process Ontology project.

## Citation

See `CITATION.cff`. After archiving on Zenodo, cite the DOI given there.
