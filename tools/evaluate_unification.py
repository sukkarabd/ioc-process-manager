"""
evaluate_unification.py — Evaluation of SQ1 (integration, requirements FR1–FR3)

Imports the same project from several source formats and reports
  1. target-field coverage per format                      (how much of the IoC target model is filled)
  2. entity/relation counts per format                      (preservation)
  3. TBox conformance violations per format                 (validity)
  4. attribute-level agreement with a reference format      (cross-format consistency)
  5. agreement of the preset-query answers (FR4–FR15) across formats

Usage:
  python tools/evaluate_unification.py sample_data/thornfield_court_sample_data.csv \
         sample_data/thornfield_court_sample_data.xlsx sample_data/thornfield_court.xml
The first file is the reference. Output: Markdown tables on stdout and in results/.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server_fastapi as s  # noqa: E402

ATTRS = ["status", "progress", "start", "end", "actor", "organisation", "predecessors",
         "resources", "materials", "outputs", "method", "space", "notes", "parent"]
FR_PRESETS = ["q01", "q02", "q03", "q05", "q06", "q07", "q08", "q10", "q13", "q16"]


def load(path: str):
    rows, report = s.detect_and_parse(path, Path(path).read_bytes())
    g, _ = s.rows_to_graph(rows, Path(path).name, "eval_" + report["format"], report)
    return g, report


def snapshot(g):
    out = {}
    for p in s._processes(g):
        d = s._extract_proc(g, p)
        sch = d["schedule"] or {}
        out[d["label"]] = {
            "status": d["status"]["label"], "progress": d["progress"],
            "start": (sch.get("startedAtTime") or "")[:10], "end": (sch.get("endedAtTime") or "")[:10],
            "actor": d["actor"]["label"] if d["actor"] else None,
            "organisation": d["actor"]["organisation"] if d["actor"] else None,
            "predecessors": sorted(x["label"] for x in d["predecessorDetails"]),
            "resources": sorted(x["label"] for x in d["resources"]),
            "materials": sorted(x["label"] for x in d["inputElements"]),
            "outputs": sorted(x["label"] for x in d["outputElements"]),
            "method": d["method"]["label"] if d["method"] else None, "space": d["initialSpace"],
            "notes": d["inputInformation"][0]["label"] if d["inputInformation"] else None,
            "parent": d["parent"]["label"] if d["parent"] else None}
    return out


def preset_answers(g):
    opts = s.preset_options(g)                       # v3.1: presets are templates; default values filled in
    res = {}
    for qid in FR_PRESETS:
        query, _ = s.fill_preset(s.PRESET_BY_ID[qid], {}, opts)
        rows = list(g.query(query))
        # compare process-level content: drop node URIs, reduce timestamps to dates
        res[qid] = sorted(tuple(str(v)[:10] if re.match(r"\d{4}-\d{2}-\d{2}T", str(v)) else str(v)
                                for v in r if v is not None and not str(v).startswith("urn:")) for r in rows)
    return res


def main(paths):
    data = {}
    for p in paths:
        g, rep = load(p)
        val = s.validate_abox(g)
        data[rep["format"]] = {"graph": g, "report": rep, "validation": val, "snap": snapshot(g), "answers": preset_answers(g)}
    fmts = list(data)
    ref = fmts[0]
    out = ["## Table A — Coverage, preservation and TBox conformance\n",
           "| Metric | " + " | ".join(fmts) + " |", "|---|" + "---|" * len(fmts),
           "| Target-field coverage | " + " | ".join(f"{data[f]['report']['targetCoverage']['ratio']:.0%} "
           f"({len(data[f]['report']['targetCoverage']['covered'])}/{len(s.TARGET_FIELDS)})" for f in fmts) + " |"]
    for k in ["processes", "summaryProcesses", "dependencies", "actors", "resources", "materials", "locations", "triples"]:
        out.append(f"| {k} | " + " | ".join(str(data[f]["report"]["counts"][k]) for f in fmts) + " |")
    out.append("| TBox violations | " + " | ".join(str(data[f]["validation"]["violations"]) for f in fmts) + " |")
    out.append("| Missing target fields | " + " | ".join(", ".join(data[f]["report"]["targetCoverage"]["missing"]) or "–" for f in fmts) + " |")

    out.append(f"\n## Table B — Attribute agreement with reference format ({ref}), matched by process label\n")
    others = fmts[1:]
    out += ["| Attribute | " + " | ".join(others) + " |", "|---|" + "---|" * len(others)]
    agreement = {}
    for a in ATTRS:
        cells = []
        for f in others:
            common = set(data[ref]["snap"]) & set(data[f]["snap"])
            both = [k for k in common if data[f]["snap"][k][a] not in (None, [], "") or data[ref]["snap"][k][a] not in (None, [], "")]
            if not both:
                cells.append("n/a"); continue
            eq = sum(1 for k in both if data[ref]["snap"][k][a] == data[f]["snap"][k][a])
            agreement.setdefault(f, {})[a] = eq / len(both)
            cells.append(f"{eq}/{len(both)} ({eq / len(both):.0%})")
        out.append(f"| {a} | " + " | ".join(cells) + " |")

    out += ["\n## Table C — Preset-query answers identical to reference\n",
            "| Preset | FR | " + " | ".join(others) + " |", "|---|---|" + "---|" * len(others)]
    frmap = {p["id"]: p["fr"] for p in s.SPARQL_PRESETS}
    for qid in FR_PRESETS:
        out.append(f"| {qid} | {frmap[qid]} | " + " | ".join(
            ("yes" if data[f]["answers"][qid] == data[ref]["answers"][qid] else f"no ({len(data[f]['answers'][qid])} vs {len(data[ref]['answers'][qid])} rows)")
            for f in others) + " |")
    report = "\n".join(out)
    print(report)
    res = ROOT / "results"; res.mkdir(exist_ok=True)
    (res / "SQ1_integration.md").write_text(report, encoding="utf-8")
    (res / "SQ1_integration.json").write_text(json.dumps({f: {"report": data[f]["report"],
        "validation": {k: v for k, v in data[f]["validation"].items() if k != "issues"},
        "agreement": agreement.get(f)} for f in fmts}, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
