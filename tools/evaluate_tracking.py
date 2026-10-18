"""
evaluate_tracking.py — Evaluation of SQ2 (tracking and delay impact, requirements FR4–FR15)

Ground truth is computed directly from the CSV with the Python standard library
only — no rdflib, no mapping code, no SPARQL, and a different algorithm for the
delay propagation (fixed-point iteration instead of the topological pass used by
the prototype) — so that it is independent of the implementation under test.
The system answers are obtained through the REST API on ABoxes imported from
CSV, XLSX and MS Project XML.

Two states are evaluated:
  S0  directly after import
  S1  after a scripted sequence of status updates (applied through the API to each
      ABox and, in parallel, to the ground-truth model) — this also tests the write path

For each requirement, format and state, precision and recall of the answer are reported.
An empty answer against an empty ground truth counts as P = R = 1.

v3.1 additions
  * the presets are parameterised: the script passes the actor / process / date values as
    parameters (POST /api/projects/{pid}/presets/{qid}/run) instead of relying on values
    written into the query text;
  * parameter sweep: FR4 for every actor, FR7/FR8/FR11 for every process, FR9 for every
    month end and FR10 for every month of the project, each against the ground truth —
    this shows that the answers are not tied to the scenario values;
  * FR13 is answered by the progress endpoint behind the Process Board (no SPARQL);
  * FR16 (versioned status change with effective date) and FR17 (re-plan; started processes
    keep their start) are checked through the API.

Answering route and interaction steps in the GUI cannot be measured automatically;
they belong to SQ3 and are recorded manually in results/SQ3_walkthrough_protocol.csv
(template created here, rows FR4–FR17).

Usage:
  python tools/evaluate_tracking.py sample_data/thornfield_court_sample_data.csv \
      sample_data/thornfield_court_sample_data.xlsx sample_data/thornfield_court.xml
"""
import base64
import csv
import json
import re
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── parameters shared with the preset queries ──────────────────────────────
ACTOR_FR4 = "Maria Okonkwo"
TARGET_FR7 = "Drainage — Below Ground Foul & Surface Water"
TARGET_FR8 = "Structural Screed Topping — Floors 1-3"
TARGET_FR11 = "Thermal & Acoustic Insulation — Floors & Partitions"
REF_DATE_FR9 = "2025-10-01"
WINDOW_FR10 = ("2025-09-01", "2025-11-01")
TARGET_FR14, DELAY_FR14 = "1st Fix Plumbing — Pipework & Waste", 21
REF_DATE_FR15 = "2025-10-01"

# S1: the scripted update sequence (label, status, progress, by, date it happened)
UPDATES = [
    ("External Render & Insulated Cladding", "finished", 100, "Site Manager", "2025-09-29"),
    (TARGET_FR11, "started", 20, "Site Manager", "2025-09-30"),
    (TARGET_FR11, "paused", None, "Site Manager", "2025-10-02"),
]

FR_PRESET = {"FR4": "q01", "FR5": "q05", "FR6": "q06", "FR7": "q07", "FR8": "q08", "FR9": "q10",
             "FR10": "q13", "FR11": "q18", "FR12": "q16", "FR13": "API /progress",
             "FR14": "API /impact", "FR15": "API /delays"}
PROJECT_ROW = "__project__"
PROJECT_END = "__project end__"


# ════════════════════════════════════════════════════════════════════════════
#  Ground truth — plain Python on the CSV
# ════════════════════════════════════════════════════════════════════════════
def split_list(v):
    return [x.strip() for x in re.split(r"[;,]", v or "") if x.strip()]


class GroundTruth:
    def __init__(self, csv_path):
        rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
        self.p = {}
        for r in rows:
            prog = int(float(r.get("progress") or 0))
            self.p[r["process_id"]] = {
                "label": r["name"], "actor": r["actor"], "start": r["start_date"][:10], "end": r["end_date"][:10],
                "preds": split_list(r["predecessors"]), "materials": split_list(r["input_materials"]),
                "space": r["space"], "status": r["status"].strip().lower(), "progress": prog,
                "history": [(r["status"].strip().lower(), prog)]}
        self.by_label = {v["label"]: k for k, v in self.p.items()}

    # status semantics, written from the IoC flag definitions
    def finished(self, i): return self.p[i]["status"] == "finished"
    def started(self, i): return self.p[i]["status"] in ("started", "paused", "finished")
    def cancelled(self, i): return self.p[i]["status"] == "cancelled"
    def done(self, i): return self.finished(i) or self.cancelled(i)
    def L(self, i): return self.p[i]["label"]

    def update(self, label, status, progress, when=None):
        i = self.by_label[label]
        if progress is None:
            progress = 100 if status == "finished" else self.p[i]["progress"]
        self.p[i]["status"], self.p[i]["progress"] = status, progress
        self.p[i]["history"].append((status, progress))
        if status in ("started", "paused") and not self.p[i].get("actual_start"):
            self.p[i]["actual_start"] = when

    # delay propagation — fixed-point iteration (Bellman-Ford style longest path)
    def propagate(self, initial):
        D = date.fromisoformat
        def gap(p, s):
            return max(0, (D(self.p[s]["start"]) - D(self.p[p]["end"])).days - 1)
        shift = {k: v for k, v in initial.items() if v > 0}
        for _ in range(len(self.p)):
            changed = False
            for s, v in self.p.items():
                if self.done(s):
                    continue
                best = max([shift.get(p, 0) - gap(p, s) for p in v["preds"]] + [initial.get(s, 0), 0])
                if best > shift.get(s, 0):
                    shift[s] = best; changed = True
            if not changed:
                break
        end_old = max(D(v["end"]) for v in self.p.values())
        end_new = max(D(v["end"]) + timedelta(days=shift.get(k, 0)) for k, v in self.p.items())
        ans = {(self.L(k), d) for k, d in shift.items() if k not in initial}
        ans.add((PROJECT_END, (end_new - end_old).days))
        return ans

    # parameterised ground truth, used by the main run and by the parameter sweep
    def succ(self):
        return {i: [j for j in self.p if i in self.p[j]["preds"]] for i in self.p}

    def fr4(self, actor):
        return {self.L(i) for i in self.p if self.p[i]["actor"] == actor and not self.finished(i) and not self.cancelled(i)}

    def fr7(self, pid):
        succ, seen, stack = self.succ(), set(), [pid]
        while stack:
            for j in succ[stack.pop()]:
                if j not in seen:
                    seen.add(j); stack.append(j)
        return {self.L(j) for j in seen}

    def fr8(self, pid):
        return {(self.L(j), self.finished(j)) for j in self.p[pid]["preds"]}

    def fr9(self, ref):
        return {self.L(i) for i in self.p if self.p[i]["end"] < ref and not self.finished(i) and not self.cancelled(i)}

    def fr10(self, a, b):
        return {(self.L(i), m) for i in self.p if a <= self.p[i]["start"] < b for m in self.p[i]["materials"]}

    def answers(self):
        P, ids = self.p, list(self.p)
        succ = {i: [j for j in ids if i in P[j]["preds"]] for i in ids}
        def transitive_succ(i):
            seen, stack = set(), [i]
            while stack:
                for j in succ[stack.pop()]:
                    if j not in seen:
                        seen.add(j); stack.append(j)
            return seen
        t4, t5, t8, t11 = (self.by_label[x] for x in (TARGET_FR7, TARGET_FR8, TARGET_FR11, TARGET_FR14))
        ref = date.fromisoformat(REF_DATE_FR15)
        late = {}
        for i in ids:
            if self.done(i):
                continue
            e, s = date.fromisoformat(P[i]["end"]), date.fromisoformat(P[i]["start"])
            if not self.started(i) and s < ref:      # cannot start before ref
                late[i] = (ref - s).days
            elif e < ref:                            # started, cannot finish before ref
                late[i] = (ref - e).days
        return {
            "FR4": {self.L(i) for i in ids if P[i]["actor"] == ACTOR_FR4 and not self.finished(i) and not self.cancelled(i)},
            "FR5": {self.L(i) for i in ids if not self.started(i) and not self.cancelled(i)
                    and all(self.finished(j) for j in P[i]["preds"])},
            "FR6": {(self.L(i), self.L(j)) for i in ids if not self.started(i) for j in P[i]["preds"] if not self.finished(j)},
            "FR7": {self.L(j) for j in transitive_succ(t4)},
            "FR8": {(self.L(j), self.finished(j)) for j in P[t5]["preds"]},
            "FR9": {self.L(i) for i in ids if P[i]["end"] < REF_DATE_FR9 and not self.finished(i) and not self.cancelled(i)},
            "FR10": {(self.L(i), m) for i in ids if WINDOW_FR10[0] <= P[i]["start"] < WINDOW_FR10[1] for m in P[i]["materials"]},
            "FR11": tuple(P[t8]["history"]),
            "FR12": {(P[i]["space"], self.L(i)) for i in ids if P[i]["space"]},
            "FR13": {(a, sum(1 for i in ids if P[i]["actor"] == a), sum(1 for i in ids if P[i]["actor"] == a and self.finished(i)))
                     for a in {P[i]["actor"] for i in ids if P[i]["actor"]}}
                    | {(PROJECT_ROW, len(ids), sum(1 for i in ids if self.finished(i)))},
            "FR14": self.propagate({t11: DELAY_FR14}),
            "FR15": {("late: " + self.L(i), d) for i, d in late.items()} | self.propagate(late),
        }


# ════════════════════════════════════════════════════════════════════════════
#  System answers — preset queries and delay endpoints through the REST API
# ════════════════════════════════════════════════════════════════════════════
def label_of_flags(started, finished, paused):
    if finished == "true": return "finished"
    if paused == "true": return "paused"
    if started == "true": return "started"
    return "planned"


def impact_set(rep, late_prefix=False):
    ans = {(a["label"], a["shiftDays"]) for a in rep["affected"] if not a["source"]}
    ans.add((PROJECT_END, rep["projectEnd"]["shiftDays"]))
    if late_prefix:
        ans |= {("late: " + x["label"], x["minDelayDays"]) for x in rep["late"]}
    return ans


def extract(fr, rows):
    """Turn SPARQL result rows into the same shape as the ground truth."""
    if fr in ("FR4", "FR5", "FR7", "FR9"):
        return {r["label"] for r in rows}
    if fr == "FR6":
        return {(r["label"], r["blockerLabel"]) for r in rows}
    if fr == "FR8":
        return {(r["predLabel"], r["finished"] == "true") for r in rows}
    if fr == "FR10":
        return {(r["label"], r["material"]) for r in rows}
    if fr == "FR11":
        ordered = sorted(rows, key=lambda r: r["generated"])
        return tuple((label_of_flags(r["started"], r["finished"], r["paused"]), int(r["progress"] or 0)) for r in ordered)
    if fr == "FR12":
        return {(r["space"], lbl) for r in rows for lbl in r["processes"].split(" | ") if lbl}
    if fr == "FR13":                                       # rows = progress endpoint response
        return ({(a["actor"], a["total"], a["finished"]) for a in rows["byActor"]}
                | {(PROJECT_ROW, rows["overall"]["total"], rows["overall"]["finished"])})


def pr(system, truth):
    if isinstance(truth, tuple):                           # FR11: sequence must match exactly
        return (1.0, 1.0) if system == truth else (0.0, 0.0)
    if not truth and not system:
        return 1.0, 1.0
    tp = len(system & truth)
    precision = tp / len(system) if system else (1.0 if not truth else 0.0)
    recall = tp / len(truth) if truth else 1.0
    return precision, recall


PRESET_PARAMS = {  # which scenario value goes into which preset parameter (process values by label)
    "q01": {"actor": ACTOR_FR4}, "q07": {"process": TARGET_FR7}, "q08": {"process": TARGET_FR8},
    "q10": {"date": REF_DATE_FR9}, "q13": {"from": WINDOW_FR10[0], "to": WINDOW_FR10[1]},
    "q18": {"process": TARGET_FR11}}


def run_preset(c, pid, qid, params):
    res = c.post(f"/api/projects/{pid}/presets/{qid}/run", json={"params": params}).json()
    return res


def check_fr16(c, pid, labels, gt):
    """Versioned status change: one state per change, old states retyped and invalidated,
    revision chain, effective dates and actual start recorded."""
    h = c.get(f"/api/projects/{pid}/processes/{labels[TARGET_FR11]}/history").json()
    v = sorted(h["versions"], key=lambda x: x["generatedAtTime"])
    ttl = c.get(f"/api/projects/{pid}/rdf").text
    expected_dates = [None] + [u[4] for u in UPDATES if u[0] == TARGET_FR11]
    checks = {
        "one state per change": len(v) == 1 + sum(1 for u in UPDATES if u[0] == TARGET_FR11),
        "exactly one current state": sum(1 for x in v if x["current"]) == 1 and v[-1]["current"],
        "old states invalidated": all(x["invalidatedAtTime"] for x in v[:-1]),
        "revision chain in RDF": ttl.count("prov:wasRevisionOf") >= len(v) - 1,
        "effective dates recorded": [x["effectiveDate"] for x in v] == expected_dates,
        "actual start = date of start": h["comparison"]["actualStart"] == gt.p[gt.by_label[TARGET_FR11]].get("actual_start"),
    }
    return checks


def check_fr17(c, pid, labels, gt):
    """Re-plan: new schedule states with the ground-truth shifts; a started process keeps its
    start; the baseline stays queryable; the ABox still conforms to the TBox."""
    before = {p["label"]: p for p in c.get(f"/api/projects/{pid}/processes").json()["processes"]}
    truth = {k: d for k, d in gt.propagate({gt.by_label[TARGET_FR14]: DELAY_FR14}) if k != PROJECT_END}
    truth[TARGET_FR14] = DELAY_FR14
    rep = c.post(f"/api/projects/{pid}/impact/apply", json={"process": labels[TARGET_FR14], "delayDays": DELAY_FR14,
                                                              "by": "SQ2 script"}).json()
    after = {p["label"]: p for p in c.get(f"/api/projects/{pid}/processes").json()["processes"]}
    D = date.fromisoformat
    def ends_ok():
        for lbl, d in truth.items():
            if D(after[lbl]["schedule"]["endedAtTime"][:10]) - D(before[lbl]["schedule"]["endedAtTime"][:10]) != timedelta(days=d):
                return False
        return True
    def starts_ok():
        for lbl, d in truth.items():
            moved = D(after[lbl]["schedule"]["startedAtTime"][:10]) - D(before[lbl]["schedule"]["startedAtTime"][:10])
            started = before[lbl]["status"]["isStarted"] and not before[lbl]["status"]["isFinished"]
            if moved != timedelta(days=0 if started else d):
                return False
        return True
    unchanged = all(after[l]["schedule"]["endedAtTime"] == before[l]["schedule"]["endedAtTime"] for l in before if l not in truth)
    q19 = c.post(f"/api/projects/{pid}/presets/q19/run", json={"params": {}}).json()
    return {
        "schedule states written = GT": rep.get("written") == len(truth),
        "new finish dates = GT": ends_ok(),
        "started keep start, others move": starts_ok(),
        "other processes unchanged": unchanged,
        "baseline still queryable (q19)": q19["count"] >= len(truth),
        "ABox conforms after write": c.get(f"/api/projects/{pid}/validate").json()["violations"] == 0,
    }


def sweep(c, pid, labels, gt):
    """Parameter sweep: the parameterised presets answered for every actor / process / period."""
    ids_by_label = labels
    months = sorted({v["start"][:7] for v in gt.p.values()} | {v["end"][:7] for v in gt.p.values()})
    def month_end(m):
        y, mo = map(int, m.split("-"))
        nxt = date(y + (mo == 12), mo % 12 + 1, 1)
        return nxt.isoformat()
    out = {}
    def run(fr, qid, cases):
        ok = 0
        for params, truth in cases:
            res = run_preset(c, pid, qid, params)
            if res["success"] and extract(fr, res["results"]) == truth:
                ok += 1
        out[fr] = (ok, len(cases))
    actors = sorted({v["actor"] for v in gt.p.values() if v["actor"]})
    run("FR4", "q01", [({"actor": a}, gt.fr4(a)) for a in actors])
    run("FR7", "q07", [({"process": ids_by_label[gt.L(i)]}, gt.fr7(i)) for i in gt.p])
    run("FR8", "q08", [({"process": ids_by_label[gt.L(i)]}, gt.fr8(i)) for i in gt.p])
    run("FR9", "q10", [({"date": month_end(m)}, gt.fr9(month_end(m))) for m in months])
    run("FR10", "q13", [({"from": m + "-01", "to": month_end(m)}, gt.fr10(m + "-01", month_end(m))) for m in months])
    run("FR11", "q18", [({"process": ids_by_label[gt.L(i)]}, tuple(gt.p[i]["history"])) for i in gt.p])
    return out


def main(paths):
    from fastapi.testclient import TestClient
    import server_fastapi as s
    c = TestClient(s.app)

    gt = GroundTruth(paths[0])                              # the CSV is the reference source
    truth = {"S0": gt.answers()}
    gt_s0 = GroundTruth(paths[0])
    for label, st, prog, _, when in UPDATES:
        gt.update(label, st, prog, when)
    truth["S1"] = gt.answers()

    results, pids, detail, fr16, fr17, sweeps = {}, [], {}, {}, {}, {}
    for path in paths:
        data = base64.b64encode(Path(path).read_bytes()).decode()
        r = c.post("/api/import/file", json={"name": "SQ2 " + Path(path).name, "filename": Path(path).name, "data": data}).json()
        pid, fmt = r["id"], r["report"]["format"]
        pids.append(pid)
        procs = c.get(f"/api/projects/{pid}/processes").json()["processes"]
        labels = {p["label"]: p["id"] for p in procs}
        sweeps[fmt] = sweep(c, pid, labels, gt_s0)
        for state in ("S0", "S1"):
            if state == "S1":
                for label, st, prog, by, when in UPDATES:
                    c.post(f"/api/projects/{pid}/processes/{labels[label]}/status",
                           json={"status": st, "progress": prog, "by": by, "date": when})
            for fr, qid in FR_PRESET.items():
                err = None
                if fr == "FR14":
                    res = c.post(f"/api/projects/{pid}/impact", json={"process": labels[TARGET_FR14], "delayDays": DELAY_FR14})
                    system = impact_set(res.json()) if res.status_code == 200 else None
                    err = None if res.status_code == 200 else res.text
                    if state == "S0" and fmt == "csv":
                        detail["FR14"] = res.json()
                elif fr == "FR15":
                    res = c.get(f"/api/projects/{pid}/delays", params={"date": REF_DATE_FR15})
                    system = impact_set(res.json(), late_prefix=True) if res.status_code == 200 else None
                    err = None if res.status_code == 200 else res.text
                    if state == "S0" and fmt == "csv":
                        detail["FR15"] = res.json()
                elif fr == "FR13":
                    res = c.get(f"/api/projects/{pid}/progress")
                    system = extract(fr, res.json()) if res.status_code == 200 else None
                    err = None if res.status_code == 200 else res.text
                else:
                    params = {k: (labels.get(v, v) if k == "process" else v)
                              for k, v in PRESET_PARAMS.get(qid, {}).items()}
                    res = run_preset(c, pid, qid, params)
                    system = extract(fr, res["results"]) if res.get("success") else None
                    err = res.get("error") or res.get("detail")
                p_, r_ = pr(system, truth[state][fr]) if system is not None else (0.0, 0.0)
                results[(fmt, state, fr)] = {"precision": p_, "recall": r_,
                                             "system": len(system) if system is not None else None,
                                             "truth": len(truth[state][fr]), "error": err}
        fr16[fmt] = check_fr16(c, pid, labels, gt)
        fr17[fmt] = check_fr17(c, pid, labels, gt)
    for pid in pids:
        c.delete(f"/api/projects/{pid}")

    fmts = list(dict.fromkeys(k[0] for k in results))
    out = ["## SQ2 — Correctness of the answers to the information needs FR4–FR15 (precision / recall against CSV ground truth)\n",
           "Prototype v3.1. Preset values are passed as parameters (no values fixed in the query text).\n"]
    for state in ("S0", "S1"):
        out.append(f"\n### State {state} — {'after import' if state == 'S0' else 'after the scripted status updates'}\n")
        out.append("| FR | Answered by | |GT| | " + " | ".join(fmts) + " |")
        out.append("|---|---|---|" + "---|" * len(fmts))
        for fr, qid in FR_PRESET.items():
            cells = []
            for f in fmts:
                x = results[(f, state, fr)]
                cells.append("error" if x["error"] else f"{x['precision']:.2f} / {x['recall']:.2f}")
            out.append(f"| {fr} | {qid} | {results[(fmts[0], state, fr)]['truth']} | " + " | ".join(cells) + " |")
    n = len(FR_PRESET) * 2
    correct = {f: sum(1 for (ff, st, fr), x in results.items() if ff == f and x["precision"] == 1 and x["recall"] == 1)
               for f in fmts}
    out.append(f"\n**Fully correct answers (P = R = 1) out of {n} ({len(FR_PRESET)} requirements × 2 states):** " +
               ", ".join(f"{f}: {k}" for f, k in correct.items()))

    out.append("\n### Parameter sweep (S0) — parameterised presets answered for every value, fully correct / cases\n")
    out.append("| FR | Preset | Parameter range | " + " | ".join(fmts) + " |")
    out.append("|---|---|---|" + "---|" * len(fmts))
    ranges = {"FR4": ("q01", "every actor"), "FR7": ("q07", "every process"), "FR8": ("q08", "every process"),
              "FR9": ("q10", "every month end"), "FR10": ("q13", "every month"), "FR11": ("q18", "every process")}
    for fr, (qid, rng) in ranges.items():
        out.append(f"| {fr} | {qid} | {rng} | " + " | ".join(f"{sweeps[f][fr][0]}/{sweeps[f][fr][1]}" for f in fmts) + " |")

    for name, chk in (("FR16 — versioned status change (S1)", fr16), ("FR17 — re-plan of the FR14 delay (after S1)", fr17)):
        out.append(f"\n### {name}\n")
        out.append("| Check | " + " | ".join(fmts) + " |")
        out.append("|---|" + "---|" * len(fmts))
        for k in chk[fmts[0]]:
            out.append(f"| {k} | " + " | ".join("pass" if chk[f][k] else "FAIL" for f in fmts) + " |")

    if "FR14" in detail:
        d = detail["FR14"]
        out.append(f"\n### FR14 detail (CSV, S0): '{TARGET_FR14}' {DELAY_FR14} days late\n")
        out.append(f"- affected successors: {d['affectedCount']}, absorbed by float: {len(d['absorbedByFloat'])}, "
                   f"actors to inform: {len(d['actorsToInform'])}, project end: {d['projectEnd']['old']} → {d['projectEnd']['new']} "
                   f"(+{d['projectEnd']['shiftDays']} days)")
        src = next(a for a in d["affected"] if a["source"])
        out.append(f"- delayed process (started): start {src['oldStart']} → {src['newStart']} (kept), finish {src['oldEnd']} → {src['newEnd']}")
        out.append("- shifts: " + ", ".join(f"{a['label']} +{a['shiftDays']} d"
                                            + (f" ({a['absorbedDays']} d absorbed by float)" if a['absorbedDays'] else "")
                                            for a in d["affected"] if not a["source"]))
    if "FR15" in detail:
        d = detail["FR15"]
        out.append(f"\n### FR15 detail (CSV, S0): reference date {d['referenceDate']}\n")
        out.append(f"- late processes: {len(d['late'])} ({', '.join(x['label'] + ' +' + str(x['minDelayDays']) + ' d' for x in d['late'])}); "
                   f"knock-on: {d['affectedCount']} successors, {len(d['actorsToInform'])} actors, "
                   f"project end {d['projectEnd']['old']} → {d['projectEnd']['new']} (+{d['projectEnd']['shiftDays']} days)")
    report = "\n".join(out)
    print(report)

    res_dir = ROOT / "results"; res_dir.mkdir(exist_ok=True)
    (res_dir / "SQ2_correctness.md").write_text(report, encoding="utf-8")
    (res_dir / "SQ2_correctness.json").write_text(json.dumps({
        "answers": {"|".join(k): v for k, v in results.items()},
        "parameterSweep": {f: {fr: {"correct": a, "cases": b} for fr, (a, b) in v.items()} for f, v in sweeps.items()},
        "FR16": fr16, "FR17": fr17}, indent=2))
    proto = res_dir / "SQ3_walkthrough_protocol.csv"
    if not proto.exists():
        with open(proto, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["FR", "requirement", "route (view / preset / custom SPARQL / not answerable)",
                        "view or preset used", "interaction steps from process board", "notes"])
            q = {"FR4": f"Unfinished processes of {ACTOR_FR4}", "FR5": "Processes that can start next",
                 "FR6": "Blocked processes and blockers", "FR7": f"Transitive successors of '{TARGET_FR7}'",
                 "FR8": f"Prerequisites of '{TARGET_FR8}' finished?", "FR9": f"Overdue on {REF_DATE_FR9}",
                 "FR10": "Materials for processes starting Sep–Oct 2025", "FR11": f"Status history of '{TARGET_FR11}'",
                 "FR12": "Processes per location", "FR13": "Completion per project / phase / actor",
                 "FR14": f"Impact if '{TARGET_FR14}' is {DELAY_FR14} days late",
                 "FR15": f"Late processes on {REF_DATE_FR15} and their knock-on effect"}
            for fr, qid in FR_PRESET.items():
                w.writerow([fr, q[fr], "", qid, "", ""])
            w.writerow(["FR16", f"Record a status change of '{TARGET_FR11}' as a new version", "", "process panel / API status", "", ""])
            w.writerow(["FR17", f"Apply the {DELAY_FR14}-day delay of '{TARGET_FR14}' as a re-plan", "", "delay impact / API /impact/apply", "", ""])
    print(f"\nwritten: {res_dir / 'SQ2_correctness.md'}")


if __name__ == "__main__":
    main(sys.argv[1:])
