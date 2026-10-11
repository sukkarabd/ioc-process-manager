"""
server_fastapi.py — IoC Process Manager (v3)
=============================================
FastAPI backend that separates the knowledge base into
  * TBox — the IoC ontology v0.5.1 (ioc_tbox.ttl, loaded read-only at start-up)
  * ABox — one RDF graph of project instance data per project (./projects/*.ttl)

Pipeline:  source file (CSV | XLSX | MS Project XML)
             → format adapter → common row model      (syntactic unification)
             → rows_to_graph()  → IoC-compliant ABox    (semantic unification)
             → delay-impact propagation over the ABox   (limiting cascading delays)
             → REST API + GUI                           (access for machines and humans)

What is derived from the TBox at run time (not hardcoded):
  * ontology browser: classes, hierarchy, labels, comments, properties,
    domains/ranges, characteristics (functional, transitive, inverse)
  * validation of every ABox against the TBox axioms (/validate)
  * instance counting through the rdfs:subClassOf hierarchy
What remains hardcoded (by design, documented in the thesis):
  * the source-to-IoC mapping (which column goes to which property path)
  * the translation between status labels and the five IoC boolean flags
  * the scheduling logic of the delay propagation (IoC has no link types or lags)
  * the SPARQL preset queries (tagged with the requirements FR4–FR15 of the thesis)
  * the task-oriented views of the GUI

Application-specific terms that IoC does not define (progress, project name,
summary flag) live in a separate namespace (EXT) so that the IoC TBox is
not modified and the extension stays visible.

Run:
  pip install fastapi uvicorn rdflib openpyxl
  uvicorn server_fastapi:app --reload --port 8000
  open http://localhost:8000
"""

import base64
import csv
import io
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel
from rdflib import XSD, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, PROV, RDF, RDFS

# ── Namespaces ──────────────────────────────────────────────────────────────

IOC   = Namespace("http://w3id.org/ioc#")
VCARD = Namespace("http://www.w3.org/2006/vcard/ns#")
EXT   = Namespace("urn:iocpm:ext#")        # application vocabulary, NOT part of IoC

# ── App setup ───────────────────────────────────────────────────────────────

app = FastAPI(title="IoC Process Manager", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR   = Path(__file__).resolve().parent
DATA_DIR   = Path("./projects")
STATIC_DIR = Path("./static")
DATA_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

# ── TBox (loaded once, never written) ───────────────────────────────────────

TBOX_PATH = Path(os.environ.get("IOC_TBOX", BASE_DIR / "ioc_tbox.ttl"))   # env override (e.g. to load a modified copy of the TBox)
if not TBOX_PATH.exists():
    raise RuntimeError(f"IoC TBox not found at {TBOX_PATH}. Place ioc_tbox.ttl next to this file.")
TBOX = Graph()
TBOX.parse(str(TBOX_PATH), format="turtle")
TBOX_VERSION = str(TBOX.value(URIRef("http://w3id.org/ioc"), OWL.versionInfo) or "unknown")


def _named_classes_of(node) -> List[URIRef]:
    """Resolve a domain/range node to named classes (handles owl:unionOf)."""
    if isinstance(node, URIRef):
        return [node]
    union = TBOX.value(node, OWL.unionOf)
    if union is not None:
        return [c for c in Collection(TBOX, union) if isinstance(c, URIRef)]
    return []


def _superclasses(cls: URIRef) -> set:
    """Reflexive-transitive closure of rdfs:subClassOf over named classes."""
    seen, stack = {cls}, [cls]
    while stack:
        c = stack.pop()
        for sup in TBOX.objects(c, RDFS.subClassOf):
            if isinstance(sup, URIRef) and sup not in seen:
                seen.add(sup)
                stack.append(sup)
    return seen


TBOX_CLASSES    = {c for c in TBOX.subjects(RDF.type, OWL.Class) if isinstance(c, URIRef)}
TBOX_OBJ_PROPS  = set(TBOX.subjects(RDF.type, OWL.ObjectProperty))
TBOX_DAT_PROPS  = set(TBOX.subjects(RDF.type, OWL.DatatypeProperty))
TBOX_ANN_PROPS  = set(TBOX.subjects(RDF.type, OWL.AnnotationProperty))
TBOX_FUNCTIONAL = set(TBOX.subjects(RDF.type, OWL.FunctionalProperty))
SUPER = {c: _superclasses(c) for c in TBOX_CLASSES}
DISJOINT = {(a, b) for a, b in TBOX.subject_objects(OWL.disjointWith)}
DISJOINT |= {(b, a) for a, b in DISJOINT}


def _qname(uri) -> str:
    s = str(uri)
    for pfx, ns in (("ioc", IOC), ("prov", PROV), ("xsd", XSD), ("rdfs", RDFS), ("vcard", VCARD),
                    ("ext", EXT), ("bot", "https://w3id.org/bot#"), ("cto", "https://w3id.org/cto#"),
                    ("ifc", "https://standards.buildingsmart.org/IFC/DEV/IFC4/ADD2_TC1/OWL#"),
                    ("owl", OWL), ("rdf", RDF)):
        if s.startswith(str(ns)):
            return f"{pfx}:{s[len(str(ns)):]}"
    return s

# ── Pydantic models ─────────────────────────────────────────────────────────

class CreateProjectRequest(BaseModel):
    name: str

class AddProcessRequest(BaseModel):
    id: Optional[str] = None
    label: str
    actor: Optional[str] = None
    actorOrg: Optional[str] = None
    method: Optional[str] = None
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    status: str = "planned"
    progress: int = 0
    space: Optional[str] = None
    predecessors: List[str] = []

class StatusUpdateRequest(BaseModel):
    status: str
    progress: Optional[int] = None
    by: Optional[str] = None          # optional actor label → ioc:generatedByActor

class SparqlQueryRequest(BaseModel):
    query: str

class ImpactRequest(BaseModel):
    process: str                      # internal process mark or URI fragment
    delayDays: Optional[int] = None   # either a delay in days …
    newEndDate: Optional[str] = None  # … or a new (forecast) end date
    by: Optional[str] = None          # only used by /impact/apply

# ── RDF / storage helpers ───────────────────────────────────────────────────

def _new_graph() -> Graph:
    g = Graph()
    for p, ns in (("ioc", IOC), ("prov", PROV), ("xsd", XSD), ("rdfs", RDFS), ("vcard", VCARD), ("ext", EXT)):
        g.bind(p, ns)
    return g

def _project_path(pid: str) -> Path: return DATA_DIR / f"{pid}.ttl"
def _meta_path(pid: str) -> Path:    return DATA_DIR / f"{pid}.meta.json"

def _check_pid(pid: str):
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", pid):
        raise HTTPException(400, "Invalid project id")

def _load_graph(pid: str) -> Graph:
    _check_pid(pid)
    path = _project_path(pid)
    if not path.exists():
        raise HTTPException(404, f"Project not found: {pid}")
    g = _new_graph()
    g.parse(str(path), format="turtle")
    return g

def _save_graph(pid: str, g: Graph):
    g.serialize(destination=str(_project_path(pid)), format="turtle")

def _save_meta(pid: str, name: str, process_count: int, existing_meta: dict = None, **extra):
    meta = existing_meta or {}
    now  = datetime.now(timezone.utc).isoformat()
    meta.setdefault("created", now)
    meta.update(name=name, modified=now, processes=process_count, **extra)
    _meta_path(pid).write_text(json.dumps(meta), encoding="utf-8")

def _load_meta(pid: str) -> dict:
    mp = _meta_path(pid)
    if mp.exists():
        try:
            return json.loads(mp.read_text())
        except Exception:
            pass
    return {}

def _slug(text: str) -> str:
    return re.sub(r"[^\w]", "_", str(text).strip())[:64]

def _now_lit() -> Literal:
    return Literal(datetime.now(timezone.utc).isoformat(timespec="milliseconds"), datatype=XSD.dateTime)

def _datetime_lit(value: str) -> Optional[Literal]:
    """IoC declares xsd:dateTime as range of prov:startedAtTime/endedAtTime."""
    v = str(value).strip()
    if not v:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v[:10]) and len(v) <= 10:
        v = v[:10] + "T00:00:00"
    return Literal(v[:19], datatype=XSD.dateTime)

def _base(pid: str) -> str:
    return f"urn:ioc:{pid}:"

def _is_summary(g: Graph, proc) -> bool:
    lit = g.value(proc, EXT.isSummary)
    return bool(lit.toPython()) if lit is not None else False

def _processes(g: Graph, include_summary: bool = False) -> list:
    return [p for p in g.subjects(RDF.type, IOC.Process) if include_summary or not _is_summary(g, p)]

def _find_process(g: Graph, proc_id: str):
    for proc in g.subjects(RDF.type, IOC.Process):
        mark = str(g.value(proc, IOC.hasInternalProcessMark) or "")
        frag = str(proc).split(":")[-1].split("#")[-1]
        if proc_id in (mark, frag):
            return proc
    return None

# ── ProcessDataState helpers (IoC versioning pattern) ───────────────────────

def _is_current(g: Graph, pds) -> bool:
    return (pds, RDF.type, IOC.CurrentProcessDataState) in g

def _current_states(g: Graph, proc, prop) -> list:
    """All *current* PDS nodes of a process for a given IoC property.
    Legacy graphs without a Current type are tolerated."""
    states = list(g.objects(proc, prop))
    cur = [s for s in states if _is_current(g, s)]
    return cur if cur else [s for s in states if (s, RDF.type, IOC.ProcessDataState) not in g]

def _current_value(g: Graph, proc, prop, value_prop):
    for pds in _current_states(g, proc, prop):
        v = g.value(pds, value_prop)
        if v is not None:
            return v
    return None

def _add_pds(g: Graph, proc, prop, value_prop, value, pds_uri: URIRef, ts: Literal):
    g.add((proc, prop, pds_uri))
    g.add((pds_uri, RDF.type, IOC.CurrentProcessDataState))
    g.add((pds_uri, PROV.generatedAtTime, ts))
    g.add((pds_uri, value_prop, value))

def _new_version(g: Graph, pid: str, proc, prop, value_prop, kind: str, by: Optional[str]):
    """Retire the current PDS of `prop` (kept as history) and create a new current PDS
    plus a new value node. Returns (new_pds, new_value_node, old_pds)."""
    old_pds = next(iter(_current_states(g, proc, prop)), None)
    ts, base = _now_lit(), _base(pid)
    n = sum(1 for _ in g.objects(proc, prop)) + 1
    frag = str(proc).split(":")[-1]
    new_pds, new_val = URIRef(f"{base}{frag}_{kind}_pds_{n}"), URIRef(f"{base}{frag}_{kind}_{n}")
    if old_pds is not None:
        g.remove((old_pds, RDF.type, IOC.CurrentProcessDataState))
        g.add((old_pds, RDF.type, IOC.ProcessDataState))
        g.add((old_pds, PROV.invalidatedAtTime, ts))
    _add_pds(g, proc, prop, value_prop, new_val, new_pds, ts)
    if old_pds is not None:
        g.add((new_pds, PROV.wasRevisionOf, old_pds))
    if by:
        actor = URIRef(f"{base}actor_{_slug(by)}")
        g.add((actor, RDF.type, IOC.Actor))
        g.add((actor, RDFS.label, Literal(by)))
        g.add((new_pds, IOC.generatedByActor, actor))
    return new_pds, new_val, old_pds

# ── Status: labels ↔ five IoC booleans (hardcoded translation) ──────────────

STATUS_FLAGS = ["isStarted", "isFinished", "isPaused", "isReady", "isCancelled"]
STATUS_VALUES = ["planned", "ready", "started", "paused", "finished", "cancelled"]

def _status_flags(status: str) -> dict:
    s = status.lower().strip()
    if s not in STATUS_VALUES:
        raise HTTPException(400, f"Unknown status '{status}'. Allowed: {STATUS_VALUES}")
    return {
        "isStarted":   s in ("started", "finished", "paused"),
        "isFinished":  s == "finished",
        "isPaused":    s == "paused",
        "isReady":     s in ("ready", "started", "finished", "paused"),
        "isCancelled": s == "cancelled",
    }

def _status_label(flags: dict) -> str:
    if flags.get("isCancelled"): return "Cancelled"
    if flags.get("isFinished"):  return "Finished"
    if flags.get("isPaused"):    return "Paused"
    if flags.get("isStarted"):   return "Started"
    if flags.get("isReady"):     return "Ready"
    return "Planned"

def _flags_of(g: Graph, status_node) -> dict:
    flags = {}
    for f in STATUS_FLAGS:
        lit = g.value(status_node, IOC[f])
        flags[f] = bool(lit.toPython()) if lit is not None else False   # bool(Literal("false")) is True!
    flags["label"] = _status_label(flags)
    return flags

def _read_status(g: Graph, proc) -> dict:
    node = _current_value(g, proc, IOC.hasStatus, IOC.hasStatusValue)
    if node is None:
        d = {f: False for f in STATUS_FLAGS}; d["label"] = "Planned"; return d
    return _flags_of(g, node)

def _read_progress(g: Graph, proc) -> Optional[int]:
    node = _current_value(g, proc, IOC.hasStatus, IOC.hasStatusValue)
    for subj, pred in ((node, EXT.progress), (proc, EXT.progress), (proc, IOC.progress)):  # last = legacy
        if subj is not None:
            lit = g.value(subj, pred)
            if lit is not None:
                return int(lit.toPython())
    return None

def _write_status(g: Graph, pid: str, proc, status: str, progress: Optional[int] = None,
                  by: Optional[str] = None) -> bool:
    """Create a NEW current status state; the previous state is kept as history
    (retyped to ioc:ProcessDataState, prov:invalidatedAtTime, linked via prov:wasRevisionOf).
    Returns False if nothing changed."""
    flags = _status_flags(status)
    old_pds = next(iter(_current_states(g, proc, IOC.hasStatus)), None)
    old_prog = None
    if old_pds is not None:
        old_node = g.value(old_pds, IOC.hasStatusValue)
        old_flags = _flags_of(g, old_node) if old_node is not None else {}
        old_prog = g.value(old_node, EXT.progress) if old_node is not None else None
        same_flags = all(old_flags.get(f) == flags[f] for f in STATUS_FLAGS)
        same_prog = progress is None or (old_prog is not None and int(old_prog) == progress)
        if same_flags and same_prog:
            return False
    _, node, _ = _new_version(g, pid, proc, IOC.hasStatus, IOC.hasStatusValue, "status", by)
    g.add((node, RDF.type, IOC.Status))
    for flag, val in flags.items():
        g.add((node, IOC[flag], Literal(val, datatype=XSD.boolean)))
    if progress is None:
        progress = 100 if flags["isFinished"] else (int(old_prog) if old_prog is not None else 0)
    g.add((node, EXT.progress, Literal(int(progress), datatype=XSD.integer)))
    return True

# ── Schedule: read and versioned write ──────────────────────────────────────

def _d(lit) -> Optional[date]:
    try:
        return date.fromisoformat(str(lit)[:10]) if lit is not None else None
    except ValueError:
        return None

def _schedule_dates(g: Graph, proc, current: bool = True) -> Tuple[Optional[date], Optional[date]]:
    if current:
        sv = _current_value(g, proc, IOC.hasSchedule, IOC.hasScheduleValue)
    else:  # baseline = the oldest schedule state
        states = sorted(g.objects(proc, IOC.hasSchedule), key=lambda s: str(g.value(s, PROV.generatedAtTime) or ""))
        sv = g.value(states[0], IOC.hasScheduleValue) if states else None
    if sv is None:
        return None, None
    return _d(g.value(sv, PROV.startedAtTime)), _d(g.value(sv, PROV.endedAtTime))

def _write_schedule(g: Graph, pid: str, proc, start: date, end: date, by: Optional[str] = None):
    """Re-plan: new current schedule state; the previous one is kept (baseline stays queryable)."""
    _, node, _ = _new_version(g, pid, proc, IOC.hasSchedule, IOC.hasScheduleValue, "schedule", by)
    g.add((node, RDF.type, IOC.Schedule))
    g.add((node, PROV.startedAtTime, Literal(f"{start.isoformat()}T00:00:00", datatype=XSD.dateTime)))
    g.add((node, PROV.endedAtTime, Literal(f"{end.isoformat()}T00:00:00", datatype=XSD.dateTime)))
    g.add((node, IOC.hasDuration, Literal(f"P{(end - start).days}D", datatype=XSD.duration)))

# ════════════════════════════════════════════════════════════════════════════
#  FORMAT ADAPTERS  →  common row model  (syntactic unification)
# ════════════════════════════════════════════════════════════════════════════
#
# Every adapter returns (rows, report). A row is a dict with the TARGET_FIELDS
# below; list-valued fields are Python lists. The report documents which source
# fields were mapped, which were lost, and why — this is the basis of the
# data-unification evaluation.

TARGET_FIELDS = ["process_id", "name", "method", "actor", "actor_org", "start_date", "end_date",
                 "predecessors", "resources", "input_materials", "output_elements", "space",
                 "status", "progress", "notes", "parent"]
LIST_FIELDS = {"predecessors", "resources", "input_materials", "output_elements"}

# alias table for tabular sources (CSV / XLSX): normalised header → target field
ALIASES = {
    "process_id": ["process_id", "id", "task_id"],
    "name": ["name", "process_name", "task_name"],
    "method": ["method", "method_name"],
    "actor": ["actor", "responsible"],
    "actor_org": ["actor_org", "organization", "organisation", "company"],
    "start_date": ["start_date", "start"],
    "end_date": ["end_date", "end", "finish"],
    "predecessors": ["predecessors", "depends_on"],
    "resources": ["resources", "equipment"],
    "input_materials": ["input_materials", "materials"],
    "output_elements": ["output_elements", "outputs"],
    "space": ["space", "location", "zone"],
    "status": ["status"],
    "progress": ["progress", "percent_complete"],
    "notes": ["notes", "description"],
    "parent": ["parent", "phase"],
}
HEADER_TO_TARGET = {a: t for t, al in ALIASES.items() for a in al}

def _norm(h) -> str:
    return re.sub(r"[\s\-]+", "_", str(h or "").strip().lower())

def _split(v) -> list:
    return [x.strip() for x in re.split(r"[;,]", str(v or "")) if x.strip()]

def _empty_report(fmt: str) -> dict:
    return {"format": fmt, "sourceFields": [], "mappedFields": {}, "unmappedSourceFields": [],
            "lostInformation": [], "warnings": []}

def _tabular_rows(headers: list, records: list, report: dict) -> list:
    mapping = {}
    for h in headers:
        if h is None or str(h).strip() == "":
            continue
        report["sourceFields"].append(str(h))
        t = HEADER_TO_TARGET.get(_norm(h))
        if t and t not in mapping.values():
            mapping[h] = t
            report["mappedFields"][str(h)] = t
        else:
            report["unmappedSourceFields"].append(str(h))
    rows = []
    for rec in records:
        row = {}
        for h, t in mapping.items():
            v = rec.get(h)
            if isinstance(v, (datetime, date)):
                v = v.isoformat()
            v = "" if v is None else str(v).strip()
            row[t] = _split(v) if t in LIST_FIELDS else v
        if rec.get("__phase__") and not row.get("parent"):
            row["parent"] = rec["__phase__"]
        if any(row.get(f) for f in ("process_id", "name")):
            rows.append(row)
    return rows

def parse_csv(text: str) -> Tuple[list, dict]:
    report = _empty_report("csv")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿").strip()))
    records = list(reader)
    return _tabular_rows(reader.fieldnames or [], records, report), report

def parse_xlsx(data: bytes) -> Tuple[list, dict]:
    """First sheet whose header row matches ≥3 known fields. Single-cell rows
    between data rows (e.g. 'PHASE 1 — ...') are interpreted as phases and
    become summary processes linked via ioc:hasParent."""
    import openpyxl
    report = _empty_report("xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    for ws in wb.worksheets:
        all_rows = [list(r) for r in ws.iter_rows(values_only=True)]
        hdr_idx = next((i for i, r in enumerate(all_rows[:20])
                        if sum(1 for c in r if HEADER_TO_TARGET.get(_norm(c))) >= 3), None)
        if hdr_idx is None:
            continue
        headers = all_rows[hdr_idx]
        records, phases, cur_phase = [], [], None
        for r in all_rows[hdr_idx + 1:]:
            filled = [c for c in r if c not in (None, "")]
            if len(filled) == 1 and r[0] not in (None, "") and len(str(r[0])) > 8:
                cur_phase = f"PH{len(phases) + 1:02d}"
                phases.append({"process_id": cur_phase, "name": str(r[0]).strip(), "_summary": True})
                continue
            if not filled:
                continue
            rec = dict(zip(headers, r))
            if cur_phase:
                rec["__phase__"] = cur_phase
            records.append(rec)
        rows = _tabular_rows(headers, records, report)
        if phases:
            report["mappedFields"]["(phase rows)"] = "parent → ioc:hasParent"
        report["sheet"] = ws.title
        ordered = [r for r in rows if not r.get("parent")]
        for ph in phases:                               # phase row followed by its children
            ordered += [ph] + [r for r in rows if r.get("parent") == ph["process_id"]]
        return ordered, report
    report["warnings"].append("No sheet with a recognisable header row found")
    return [], report

MSP_NS = {"p": "http://schemas.microsoft.com/project"}
MSP_LINK_TYPES = {"0": "FF", "1": "FS", "2": "SF", "3": "SS"}

def parse_mspdi(data: bytes) -> Tuple[list, dict]:
    """Microsoft Project XML (MSPDI). Mapping rules:
       Task → ioc:Process; summary tasks → parent processes (ioc:hasParent via OutlineNumber)
       Work resource (Group ≠ 'Equipment') → ioc:Actor, Group → organisation
       Work resource (Group = 'Equipment') → ioc:Resource
       Material resource (Type 0) → ioc:RawMaterial input
       PercentComplete/ActualStart → IoC status booleans"""
    report = _empty_report("mspdi")
    root = ET.fromstring(data)
    def t(el, tag):
        x = el.find(f"p:{tag}", MSP_NS)
        return x.text.strip() if x is not None and x.text else ""

    resources = {}
    for r in root.findall("p:Resources/p:Resource", MSP_NS):
        uid = t(r, "UID")
        if uid in ("", "0") or not t(r, "Name"):
            continue
        resources[uid] = {"name": t(r, "Name"), "type": t(r, "Type") or "1", "group": t(r, "Group")}
    assign = {}
    for a in root.findall("p:Assignments/p:Assignment", MSP_NS):
        assign.setdefault(t(a, "TaskUID"), []).append(t(a, "ResourceUID"))

    tasks = [x for x in root.findall("p:Tasks/p:Task", MSP_NS) if t(x, "UID") not in ("", "0")]
    seen_tags = set()
    for x in tasks:
        seen_tags |= {c.tag.split("}")[-1] for c in x}
    mapped = {"UID": "process_id", "Name": "name", "Start": "start_date", "Finish": "end_date",
              "PercentComplete": "progress + status", "ActualStart": "status", "ActualFinish": "status",
              "Notes": "notes", "PredecessorLink": "predecessors", "OutlineNumber": "parent", "Summary": "summary flag"}
    report["sourceFields"] = sorted(seen_tags | {"Resource.Name", "Resource.Type", "Resource.Group", "Assignment"})
    report["mappedFields"] = {**mapped, "Resource.Name": "actor / resources / input_materials",
                              "Resource.Type": "routing (work vs material)", "Resource.Group": "actor_org / equipment",
                              "Assignment": "task ↔ resource link"}
    ignore = {"ID", "OutlineLevel", "WBS", "Type", "IsNull", "CreateDate", "Duration", "DurationFormat"}
    report["unmappedSourceFields"] = sorted(seen_tags - set(mapped) - ignore)

    by_outline = {t(x, "OutlineNumber"): f"T{t(x, 'UID')}" for x in tasks if t(x, "OutlineNumber")}
    rows, lost_links = [], 0
    for x in tasks:
        uid, pct = t(x, "UID"), int(float(t(x, "PercentComplete") or 0))
        status = "finished" if pct >= 100 else "started" if (pct > 0 or t(x, "ActualStart")) else "planned"
        row = {"process_id": f"T{uid}", "name": t(x, "Name"),
               "start_date": t(x, "Start"), "end_date": t(x, "Finish"),
               "status": status, "progress": str(pct), "notes": t(x, "Notes"),
               "predecessors": [], "resources": [], "input_materials": [], "output_elements": []}
        on = t(x, "OutlineNumber")
        if "." in on:
            row["parent"] = by_outline.get(on.rsplit(".", 1)[0], "")
        if t(x, "Summary") == "1":
            row["_summary"] = True
        for link in x.findall("p:PredecessorLink", MSP_NS):
            row["predecessors"].append(f"T{t(link, 'PredecessorUID')}")
            ltype, lag = MSP_LINK_TYPES.get(t(link, "Type") or "1", "FS"), t(link, "LinkLag") or "0"
            if ltype != "FS" or lag not in ("0", ""):
                lost_links += 1
        people = []
        for ruid in assign.get(uid, []):
            r = resources.get(ruid)
            if not r:
                continue
            if r["type"] == "0":
                row["input_materials"].append(r["name"])
            elif r["group"].lower() == "equipment":
                row["resources"].append(r["name"])
            else:
                people.append(r)
        if people:
            row["actor"], row["actor_org"] = people[0]["name"], people[0]["group"]
            if len(people) > 1:
                report["warnings"].append(f"T{uid}: {len(people)} people assigned, only the first is mapped to ioc:hasActor")
        rows.append(row)
    if lost_links:
        report["lostInformation"].append(f"{lost_links} dependency link(s) with type ≠ FS or non-zero lag: "
                                         "ioc:hasPredecessor carries no link type or lag")
    report["lostInformation"].append("MS Project has no native fields for method, space or output elements")
    return rows, report

def detect_and_parse(filename: str, data: bytes) -> Tuple[list, dict]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        return parse_xlsx(data)
    head = data[:600].decode("utf-8", "ignore")
    if name.endswith(".xml") or "schemas.microsoft.com/project" in head:
        return parse_mspdi(data)
    return parse_csv(data.decode("utf-8-sig", "replace"))

# ════════════════════════════════════════════════════════════════════════════
#  MAPPING  common row model → IoC ABox  (semantic unification)
# ════════════════════════════════════════════════════════════════════════════
#
#  process_id      → ioc:hasInternalProcessMark  (+ URI fragment)
#  name            → rdfs:label
#  method          → ioc:hasMethod        → PDS → ioc:hasMethodValue        → ioc:Method
#  actor/actor_org → ioc:hasActor         → PDS → ioc:hasActorValue         → ioc:Actor (+vcard:hasOrganizationName)
#  start/end_date  → ioc:hasSchedule      → PDS → ioc:hasScheduleValue      → ioc:Schedule
#                     (prov:startedAtTime / prov:endedAtTime as xsd:dateTime, ioc:hasDuration)
#  resources       → ioc:hasResource      → PDS → ioc:hasResourceValue      → ioc:Resource
#  input_materials → ioc:hasInputElement  → PDS → ioc:hasInputElementValue  → ioc:RawMaterial
#  output_elements → ioc:hasOutputElement → PDS → ioc:hasOutputElementValue → ioc:Element
#  space           → ioc:hasInitialSpace  → PDS → ioc:hasInitialSpaceValue  → ioc:Location
#  status/progress → ioc:hasStatus        → PDS → ioc:hasStatusValue        → ioc:Status (5 booleans, ext:progress)
#  notes           → ioc:hasInputInformation → PDS → ioc:hasInputInformationValue → ioc:Information
#  predecessors    → ioc:hasPredecessor (asserted direction only; successors derived at query time)
#  parent          → ioc:hasParent (asserted direction only; children derived at query time)

def rows_to_graph(rows: list, project_name: str, pid: str, report: dict) -> Tuple[Graph, int]:
    g, base, ts = _new_graph(), _base(pid), _now_lit()
    proj = URIRef(f"urn:ioc:project:{pid}")
    g.add((proj, RDF.type, EXT.Project))
    g.add((proj, EXT.projectName, Literal(project_name)))
    g.add((proj, EXT.sourceFormat, Literal(report.get("format", "unknown"))))
    g.add((proj, EXT.importedAt, ts))     # not prov:generatedAtTime: its IoC domain is ioc:ProcessDataState

    proc_ids, count = {}, 0
    for i, row in enumerate(rows):
        csv_id = row.get("process_id") or f"P{i + 1:02d}"
        sid = _slug(csv_id)
        proc = URIRef(f"{base}{sid}")
        if csv_id in proc_ids:
            report["warnings"].append(f"Row {i + 1}: duplicate process id '{csv_id}' — merged")
        proc_ids[csv_id] = proc
        g.add((proc, RDF.type, IOC.Process))
        g.add((proc, RDFS.label, Literal(row.get("name") or f"Process {i + 1}")))
        g.add((proc, IOC.hasInternalProcessMark, Literal(csv_id)))
        if row.get("_summary"):
            g.add((proc, EXT.isSummary, Literal(True, datatype=XSD.boolean)))
            continue
        count += 1

        status = (row.get("status") or "planned").lower().strip()
        try:
            progress = int(float(row.get("progress") or 0))
        except ValueError:
            progress = 0
            report["warnings"].append(f"Row {i + 1}: progress '{row.get('progress')}' is not a number")
        if status == "100":
            status, progress = "finished", 100
        if status not in STATUS_VALUES:
            report["warnings"].append(f"Row {i + 1}: unknown status '{status}' → planned")
            status = "planned"
        _write_status(g, pid, proc, status, progress)

        def link(prop, vprop, value_uri, cls, label, tag):
            pds = URIRef(f"{base}{sid}_{tag}_pds")
            _add_pds(g, proc, prop, vprop, value_uri, pds, ts)
            g.add((value_uri, RDF.type, cls))
            g.add((value_uri, RDFS.label, Literal(label)))

        if row.get("actor"):
            a = URIRef(f"{base}actor_{_slug(row['actor'])}")
            link(IOC.hasActor, IOC.hasActorValue, a, IOC.Actor, row["actor"], "actor")
            if row.get("actor_org"):
                g.set((a, VCARD.hasOrganizationName, Literal(row["actor_org"])))
        if row.get("method"):
            link(IOC.hasMethod, IOC.hasMethodValue, URIRef(f"{base}method_{_slug(row['method'])}"),
                 IOC.Method, row["method"], "method")
        start, end = _datetime_lit(row.get("start_date", "")), _datetime_lit(row.get("end_date", ""))
        if start or end:
            sched = URIRef(f"{base}{sid}_schedule")
            _add_pds(g, proc, IOC.hasSchedule, IOC.hasScheduleValue, sched, URIRef(f"{base}{sid}_schedule_pds"), ts)
            g.add((sched, RDF.type, IOC.Schedule))
            if start: g.add((sched, PROV.startedAtTime, start))
            if end:   g.add((sched, PROV.endedAtTime, end))
            if start and end:
                try:
                    days = (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days
                    g.add((sched, IOC.hasDuration, Literal(f"P{days}D", datatype=XSD.duration)))
                except ValueError:
                    report["warnings"].append(f"Row {i + 1}: unparseable dates")
        for j, lbl in enumerate(row.get("resources") or []):
            link(IOC.hasResource, IOC.hasResourceValue, URIRef(f"{base}resource_{_slug(lbl)}"),
                 IOC.Resource, lbl, f"res{j}")
        for j, lbl in enumerate(row.get("input_materials") or []):
            link(IOC.hasInputElement, IOC.hasInputElementValue, URIRef(f"{base}material_{_slug(lbl)}"),
                 IOC.RawMaterial, lbl, f"in{j}")
        for j, lbl in enumerate(row.get("output_elements") or []):
            link(IOC.hasOutputElement, IOC.hasOutputElementValue, URIRef(f"{base}element_{_slug(lbl)}"),
                 IOC.Element, lbl, f"out{j}")
        if row.get("space"):
            link(IOC.hasInitialSpace, IOC.hasInitialSpaceValue, URIRef(f"{base}location_{_slug(row['space'])}"),
                 IOC.Location, row["space"], "space")
        if row.get("notes"):
            link(IOC.hasInputInformation, IOC.hasInputInformationValue, URIRef(f"{base}{sid}_info"),
                 IOC.Information, row["notes"][:500], "info")

    deps = 0
    for i, row in enumerate(rows):
        proc = proc_ids.get(row.get("process_id") or f"P{i + 1:02d}")
        for pred in row.get("predecessors") or []:
            if pred in proc_ids:
                g.add((proc, IOC.hasPredecessor, proc_ids[pred])); deps += 1
            else:
                report["warnings"].append(f"Row {i + 1}: predecessor '{pred}' not found")
        par = row.get("parent")
        if par:
            if par in proc_ids:
                g.add((proc, IOC.hasParent, proc_ids[par]))
            else:
                report["warnings"].append(f"Row {i + 1}: parent '{par}' not found")

    filled = {f for r in rows if not r.get("_summary") for f in TARGET_FIELDS if r.get(f)}
    report["targetCoverage"] = {"covered": [f for f in TARGET_FIELDS if f in filled],
                                "missing": [f for f in TARGET_FIELDS if f not in filled],
                                "ratio": round(len(filled) / len(TARGET_FIELDS), 3)}
    report["counts"] = {
        "processes": count, "summaryProcesses": sum(1 for r in rows if r.get("_summary")), "dependencies": deps,
        "actors": len(set(g.subjects(RDF.type, IOC.Actor))), "resources": len(set(g.subjects(RDF.type, IOC.Resource))),
        "materials": len(set(g.subjects(RDF.type, IOC.RawMaterial))), "locations": len(set(g.subjects(RDF.type, IOC.Location))),
        "triples": len(g)}
    return g, count

def csv_to_graph(csv_text: str, project_name: str, pid: str):
    """Backwards-compatible wrapper."""
    rows, report = parse_csv(csv_text)
    g, count = rows_to_graph(rows, project_name, pid, report)
    return g, count, report["warnings"]

# ════════════════════════════════════════════════════════════════════════════
#  DELAY-IMPACT PROPAGATION  (limiting cascading delays)
# ════════════════════════════════════════════════════════════════════════════
#
# IoC relates processes through ioc:hasPredecessor but carries no link type and no
# lag. The propagation therefore assumes finish-to-start links and works with
# calendar days. It propagates *shifts* rather than recomputing absolute dates:
#
#   gap(p→s)  = max(0, start(s) − end(p) − 1 day)        float of the link in the baseline
#   shift(s)  = max over predecessors p of  max(0, shift(p) − gap(p→s))
#
# so that a zero delay never moves anything, even where the baseline schedule
# overlaps a predecessor (then gap = 0 and a delay is passed on in full).
# Finished and cancelled processes are never moved; durations are preserved.
# SPARQL 1.1 can find all (transitive) successors, but it cannot compute this
# longest-path recursion, which is why it is computed here and exposed via the API.

def _schedule_net(g: Graph) -> dict:
    net = {}
    for p in _processes(g):
        s, e = _schedule_dates(g, p)
        st = _read_status(g, p)
        a = _current_value(g, p, IOC.hasActor, IOC.hasActorValue)
        net[p] = {"start": s, "end": e, "preds": [x for x in g.objects(p, IOC.hasPredecessor)],
                  "succs": [x for x in g.subjects(IOC.hasPredecessor, p)],
                  "done": st["isFinished"] or st["isCancelled"], "status": st["label"],
                  "label": str(g.value(p, RDFS.label) or p),
                  "id": str(g.value(p, IOC.hasInternalProcessMark) or str(p).split(":")[-1]),
                  "actor": str(g.value(a, RDFS.label)) if a is not None else None,
                  "org": str(g.value(a, VCARD.hasOrganizationName) or "") if a is not None else None}
    return net

def _gap(net, p, s) -> int:
    if net[p]["end"] is None or net[s]["start"] is None:
        return 0
    return max(0, (net[s]["start"] - net[p]["end"]).days - 1)

def propagate(net: dict, initial: dict) -> dict:
    """initial: {process: shift_days}. Returns {process: shift_days} for every process
    whose schedule changes (including the sources)."""
    shift = {p: d for p, d in initial.items() if d > 0}
    reach, stack = set(), list(shift)                      # all transitive successors of the sources
    while stack:
        for s in net[stack.pop()]["succs"]:
            if s in net and s not in reach:
                reach.add(s); stack.append(s)
    indeg = {s: sum(1 for p in net[s]["preds"] if p in reach) for s in reach}
    order, queue = [], [s for s in reach if indeg[s] == 0]
    while queue:                                           # Kahn's algorithm on the reachable subgraph
        n = queue.pop()
        order.append(n)
        for s in net[n]["succs"]:
            if s in indeg:
                indeg[s] -= 1
                if indeg[s] == 0:
                    queue.append(s)
    if len(order) < len(reach):
        raise HTTPException(422, "Dependency cycle among the affected processes — propagation not possible")
    for s in order:
        if net[s]["done"]:
            continue
        cand = max((shift.get(p, 0) - _gap(net, p, s) for p in net[s]["preds"] if p in net), default=0)
        cand = max(cand, initial.get(s, 0))
        if cand > 0:
            shift[s] = cand
    return shift

def _impact_report(net: dict, shift: dict, sources: dict) -> dict:
    ends = [v["end"] for v in net.values() if v["end"]]
    old_end = max(ends) if ends else None
    new_end = max((v["end"] + timedelta(days=shift.get(p, 0)) for p, v in net.items() if v["end"]), default=None)
    affected = []
    for p, d in sorted(shift.items(), key=lambda kv: (net[kv[0]]["start"] or date.max, net[kv[0]]["label"])):
        v = net[p]
        affected.append({"uri": str(p), "id": v["id"], "label": v["label"], "actor": v["actor"], "organisation": v["org"],
                         "status": v["status"], "shiftDays": d, "source": p in sources,
                         "oldStart": v["start"].isoformat() if v["start"] else None,
                         "oldEnd": v["end"].isoformat() if v["end"] else None,
                         "newStart": (v["start"] + timedelta(days=d)).isoformat() if v["start"] else None,
                         "newEnd": (v["end"] + timedelta(days=d)).isoformat() if v["end"] else None})
    reach, stack = set(), list(sources)
    while stack:
        for s in net[stack.pop()]["succs"]:
            if s in net and s not in reach:
                reach.add(s); stack.append(s)
    absorbed = [{"id": net[s]["id"], "label": net[s]["label"]} for s in reach
                if s not in shift and not net[s]["done"]]
    actors = {}
    for a in affected:
        if a["source"] or not a["actor"]:
            continue
        x = actors.setdefault(a["actor"], {"actor": a["actor"], "organisation": a["organisation"],
                                           "processes": 0, "maxShiftDays": 0, "firstAffectedStart": a["oldStart"]})
        x["processes"] += 1
        x["maxShiftDays"] = max(x["maxShiftDays"], a["shiftDays"])
        if a["oldStart"] and (x["firstAffectedStart"] is None or a["oldStart"] < x["firstAffectedStart"]):
            x["firstAffectedStart"] = a["oldStart"]
    return {"sources": [{"id": net[p]["id"], "label": net[p]["label"], "delayDays": d} for p, d in sources.items()],
            "affected": affected,
            "affectedCount": sum(1 for a in affected if not a["source"]),
            "absorbedByFloat": absorbed,
            "actorsToInform": sorted(actors.values(), key=lambda x: x["firstAffectedStart"] or ""),
            "projectEnd": {"old": old_end.isoformat() if old_end else None,
                           "new": new_end.isoformat() if new_end else None,
                           "shiftDays": (new_end - old_end).days if old_end and new_end else 0},
            "assumptions": "finish-to-start links, calendar days, durations preserved; "
                           "finished and cancelled processes are not moved"}

def _resolve_delay(net, proc, req: ImpactRequest) -> int:
    if req.delayDays is None and not req.newEndDate:
        raise HTTPException(400, "Give delayDays or newEndDate")
    if req.newEndDate:
        if net[proc]["end"] is None:
            raise HTTPException(400, "Process has no planned end date")
        nd = _d(req.newEndDate)
        if nd is None:
            raise HTTPException(400, "newEndDate must be YYYY-MM-DD")
        return (nd - net[proc]["end"]).days
    return int(req.delayDays)

def delay_impact(g: Graph, proc, days: int) -> dict:
    net = _schedule_net(g)
    if proc not in net:
        raise HTTPException(400, "Summary processes cannot be delayed directly")
    if net[proc]["done"]:
        raise HTTPException(400, f"Process is {net[proc]['status'].lower()} — it cannot be delayed any more")
    return _impact_report(net, propagate(net, {proc: max(0, days)}), {proc: max(0, days)})

def status_date_default(g: Graph) -> date:
    """Heuristic 'data date' when none is given: today, unless today lies outside the
    project; then the day after the latest planned end of a finished process."""
    net, today = _schedule_net(g), date.today()
    starts = [v["start"] for v in net.values() if v["start"]]
    ends = [v["end"] for v in net.values() if v["end"]]
    if starts and ends and min(starts) <= today <= max(ends):
        return today
    fin = [v["end"] for v in net.values() if v["status"] == "Finished" and v["end"]]
    return (max(fin) + timedelta(days=1)) if fin else (min(starts) if starts else today)

def current_delays(g: Graph, ref: date) -> dict:
    """Processes that are late at the reference date and their combined knock-on effect.
    Late = not finished/cancelled and (planned end < ref, or not started although planned start < ref).
    A late process cannot finish before ref, so its minimum delay is ref − planned end."""
    net = _schedule_net(g)
    initial, late = {}, []
    for p, v in net.items():
        if v["done"] or v["end"] is None:
            continue
        if v["end"] < ref:
            d, why = (ref - v["end"]).days, "planned end passed"
        elif v["status"] in ("Planned", "Ready") and v["start"] and v["start"] < ref:
            d, why = (ref - v["start"]).days, "planned start passed, not started"
        else:
            continue
        initial[p] = d
        late.append({"id": v["id"], "label": v["label"], "actor": v["actor"], "status": v["status"],
                     "plannedStart": v["start"].isoformat() if v["start"] else None,
                     "plannedEnd": v["end"].isoformat(), "minDelayDays": d, "reason": why})
    rep = _impact_report(net, propagate(net, initial), initial)
    rep["referenceDate"] = ref.isoformat()
    rep["late"] = sorted(late, key=lambda x: -x["minDelayDays"])
    return rep

# ════════════════════════════════════════════════════════════════════════════
#  VALIDATION  ABox ⟷ TBox   (closed-world check of the IoC axioms)
# ════════════════════════════════════════════════════════════════════════════
#
# OWL is interpreted under the open-world assumption: a domain axiom would make a
# reasoner *infer* a type rather than report an error. For data-quality purposes the
# checks below read the axioms as constraints (closed-world), which is the usual
# practice for validating instance data (cf. SHACL).

def validate_abox(g: Graph, max_issues: int = 200) -> dict:
    types = {}
    for s, o in g.subject_objects(RDF.type):
        types.setdefault(s, set()).update(SUPER.get(o, {o}))
    rules = {k: 0 for k in ["R1 undeclared IoC term", "R2 instance in IoC namespace", "R3 datatype range",
                            "R4 domain", "R5 range", "R6 functional property", "R7 disjoint classes"]}
    issues, ext_terms = [], set()

    def issue(rule, subj, msg):
        rules[rule] += 1
        if len(issues) < max_issues:
            issues.append({"rule": rule, "subject": str(subj), "message": msg})

    for s, p, o in g:
        if str(p).startswith(str(EXT)) or (p == RDF.type and str(o).startswith(str(EXT))):
            ext_terms.add(_qname(o if p == RDF.type else p)); continue
        if str(p).startswith(str(IOC)) and p not in TBOX_OBJ_PROPS | TBOX_DAT_PROPS | TBOX_ANN_PROPS:
            issue("R1 undeclared IoC term", s, f"{_qname(p)} is not declared in IoC {TBOX_VERSION}")
        if p == RDF.type and str(o).startswith(str(IOC)) and o not in TBOX_CLASSES:
            issue("R1 undeclared IoC term", s, f"class {_qname(o)} is not declared in IoC {TBOX_VERSION}")
        if isinstance(s, URIRef) and str(s).startswith(str(IOC)):
            issue("R2 instance in IoC namespace", s, "ABox individual minted in the TBox namespace")
        if p in TBOX_DAT_PROPS and isinstance(o, Literal):
            rng = TBOX.value(p, RDFS.range)
            dt = o.datatype or (XSD.string if o.language is None else RDF.langString)
            if rng is not None and rng != RDFS.Literal and dt != rng:
                issue("R3 datatype range", s, f"{_qname(p)} has {_qname(dt)}, range is {_qname(rng)}")
        if p in TBOX_OBJ_PROPS | TBOX_DAT_PROPS:
            for dom in TBOX.objects(p, RDFS.domain):
                allowed = set(_named_classes_of(dom))
                if allowed and not (types.get(s, set()) & allowed):
                    issue("R4 domain", s, f"subject of {_qname(p)} is not typed as {', '.join(map(_qname, allowed))}")
        if p in TBOX_OBJ_PROPS:
            for rng in TBOX.objects(p, RDFS.range):
                allowed = set(_named_classes_of(rng))
                if allowed and not (types.get(o, set()) & allowed):
                    issue("R5 range", s, f"object of {_qname(p)} ({_qname(o)}) is not typed as {', '.join(map(_qname, allowed))}")
    for p in TBOX_FUNCTIONAL:
        counts = {}
        for s, o in g.subject_objects(p):
            counts[s] = counts.get(s, 0) + 1
        for s, c in counts.items():
            if c > 1:
                issue("R6 functional property", s, f"{_qname(p)} is functional but has {c} values")
    for s, ts in types.items():
        for a in ts:
            for b in ts:
                if (a, b) in DISJOINT and str(a) < str(b):
                    issue("R7 disjoint classes", s, f"typed as disjoint classes {_qname(a)} and {_qname(b)}")
    total = sum(rules.values())
    return {"conforms": total == 0, "tboxVersion": TBOX_VERSION, "triples": len(g),
            "violations": total, "byRule": rules, "issues": issues,
            "applicationExtensionTerms": sorted(ext_terms),
            "note": "Axioms of the IoC TBox checked under a closed-world reading."}

# ── Serialiser (graph → dict) ───────────────────────────────────────────────

def _label(g, node):
    return str(g.value(node, RDFS.label) or str(node).split(":")[-1])

def _brief(g, node):
    return {"uri": str(node), "id": str(g.value(node, IOC.hasInternalProcessMark) or str(node).split(":")[-1]),
            "label": _label(g, node), "status": _read_status(g, node)}

def _extract_proc(g: Graph, proc) -> dict:
    status = _read_status(g, proc)
    prog = _read_progress(g, proc)
    progress = prog if prog is not None else (100 if status["isFinished"] else 0)

    def single(prop, vprop):
        v = _current_value(g, proc, prop, vprop)
        return {"uri": str(v), "label": _label(g, v)} if v is not None else None

    def multi(prop, vprop, typed=False):
        out = []
        for pds in _current_states(g, proc, prop):
            v = g.value(pds, vprop)
            if v is None:
                continue
            item = {"uri": str(v), "label": _label(g, v)}
            if typed:
                item["type"] = next((c for c in ("RawMaterial", "AuxiliaryElement") if (v, RDF.type, IOC[c]) in g), "Element")
            out.append(item)
        return out

    schedule = None
    sv = _current_value(g, proc, IOC.hasSchedule, IOC.hasScheduleValue)
    if sv is not None:
        start, end, dur = g.value(sv, PROV.startedAtTime), g.value(sv, PROV.endedAtTime), g.value(sv, IOC.hasDuration)
        schedule = {"startedAtTime": str(start) if start else None, "endedAtTime": str(end) if end else None,
                    "hasDuration": str(dur) if dur else None, "durationDays": None,
                    "versions": sum(1 for _ in g.objects(proc, IOC.hasSchedule))}
        if start and end:
            try:
                schedule["durationDays"] = (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days
            except ValueError:
                pass
        _, bend = _schedule_dates(g, proc, current=False)
        schedule["baselineEnd"] = bend.isoformat() if bend else None
        schedule["slipDays"] = (_d(end) - bend).days if bend and _d(end) else 0

    actor = single(IOC.hasActor, IOC.hasActorValue)
    if actor:
        org = g.value(URIRef(actor["uri"]), VCARD.hasOrganizationName)
        actor["organisation"] = str(org) if org else None
    isp = single(IOC.hasInitialSpace, IOC.hasInitialSpaceValue)
    tsp = single(IOC.hasTargetSpace, IOC.hasTargetSpaceValue)
    preds = list(g.objects(proc, IOC.hasPredecessor))
    succs = list(g.subjects(IOC.hasPredecessor, proc))
    parent = g.value(proc, IOC.hasParent)
    pred_details = [_brief(g, p) for p in preds]

    return {
        "id": str(g.value(proc, IOC.hasInternalProcessMark) or str(proc).split(":")[-1]),
        "uri": str(proc), "label": _label(g, proc),
        "internalMark": str(g.value(proc, IOC.hasInternalProcessMark) or ""),
        "status": status, "progress": progress, "actor": actor,
        "method": single(IOC.hasMethod, IOC.hasMethodValue), "schedule": schedule,
        "resources": multi(IOC.hasResource, IOC.hasResourceValue),
        "inputElements": multi(IOC.hasInputElement, IOC.hasInputElementValue, True),
        "outputElements": multi(IOC.hasOutputElement, IOC.hasOutputElementValue, True),
        "inputInformation": multi(IOC.hasInputInformation, IOC.hasInputInformationValue),
        "outputInformation": multi(IOC.hasOutputInformation, IOC.hasOutputInformationValue),
        "initialSpace": isp["label"] if isp else None, "targetSpace": tsp["label"] if tsp else None,
        "parent": {"uri": str(parent), "label": _label(g, parent)} if parent is not None else None,
        "predecessors": [str(p) for p in preds], "successors": [str(s) for s in succs],
        "predecessorDetails": pred_details, "successorDetails": [_brief(g, s) for s in succs],
        "predecessorsFinished": all(d["status"]["isFinished"] for d in pred_details),
        "statusVersions": sum(1 for _ in g.objects(proc, IOC.hasStatus)),
        "isBlocking": bool(succs) and status["isStarted"] and not status["isFinished"],
    }

# ════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    for path in [BASE_DIR / "index.html", Path("index.html"), STATIC_DIR / "index.html"]:
        if path.exists():
            return HTMLResponse(path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)

# ── Projects ────────────────────────────────────────────────────────────────

@app.get("/api/projects")
async def list_projects():
    projects = []
    for f in sorted(DATA_DIR.glob("*.ttl")):
        pid, meta = f.stem, _load_meta(f.stem)
        projects.append({"id": pid, "name": meta.get("name", pid), "processes": meta.get("processes", 0),
                         "format": meta.get("format", ""), "size_kb": round(f.stat().st_size / 1024, 1),
                         "created": meta.get("created", ""), "modified": meta.get("modified", "")})
    return {"projects": projects}

@app.post("/api/projects")
async def create_project(req: CreateProjectRequest):
    pid = "proj_" + uuid.uuid4().hex[:8]
    g = _new_graph()
    proj = URIRef(f"urn:ioc:project:{pid}")
    g.add((proj, RDF.type, EXT.Project))
    g.add((proj, EXT.projectName, Literal(req.name)))
    _save_graph(pid, g)
    _save_meta(pid, req.name, 0, format="manual")
    return {"id": pid, "name": req.name, "created": True}

@app.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    _check_pid(pid)
    path = _project_path(pid)
    if not path.exists():
        raise HTTPException(404, "Project not found")
    path.unlink()
    if _meta_path(pid).exists():
        _meta_path(pid).unlink()
    return {"ok": True}

def _stats(g: Graph, procs: list) -> dict:
    total = len(procs)
    fin = sum(1 for p in procs if p["status"]["isFinished"])
    started = sum(1 for p in procs if p["status"]["isStarted"] and not p["status"]["isFinished"])
    blocked = sum(1 for p in procs if not p["status"]["isStarted"] and not p["predecessorsFinished"])
    by = {k: sum(1 for p in procs if p["status"]["label"] == k)
          for k in ("Finished", "Started", "Planned", "Ready", "Paused", "Cancelled")}
    return {"total_processes": total, "finished": fin, "in_progress": started, "blocked": blocked,
            "completion_pct": round(fin / total * 100) if total else 0,
            "actors": len({p["actor"]["label"] for p in procs if p["actor"]}), "byStatus": by}

@app.get("/api/projects/{pid}")
async def get_project(pid: str):
    g, meta = _load_graph(pid), _load_meta(pid)
    procs = [_extract_proc(g, p) for p in _processes(g)]
    st = _stats(g, procs); st["projectName"] = meta.get("name", pid)
    return {"id": pid, "name": meta.get("name", pid), "stats": st, "processes": procs}

@app.get("/api/projects/{pid}/stats")
async def get_stats(pid: str):
    return (await get_project(pid))["stats"]

# ── Processes ───────────────────────────────────────────────────────────────

@app.get("/api/projects/{pid}/processes")
async def list_processes(pid: str):
    g = _load_graph(pid)
    return {"processes": [_extract_proc(g, p) for p in _processes(g)]}

@app.get("/api/projects/{pid}/processes/{proc_id}")
async def get_process(pid: str, proc_id: str):
    g = _load_graph(pid)
    proc = _find_process(g, proc_id)
    if proc is None:
        raise HTTPException(404, "Process not found")
    return _extract_proc(g, proc)

@app.get("/api/projects/{pid}/processes/{proc_id}/history")
async def get_status_history(pid: str, proc_id: str):
    """All ioc:Status and ioc:Schedule states of a process, newest first (ProcessDataState versioning)."""
    g = _load_graph(pid)
    proc = _find_process(g, proc_id)
    if proc is None:
        raise HTTPException(404, "Process not found")
    out = []
    for pds in g.objects(proc, IOC.hasStatus):
        node = g.value(pds, IOC.hasStatusValue)
        by = g.value(pds, IOC.generatedByActor)
        prog = g.value(node, EXT.progress) if node is not None else None
        out.append({"kind": "status", "pds": str(pds), "current": _is_current(g, pds),
                    "generatedAtTime": str(g.value(pds, PROV.generatedAtTime) or ""),
                    "invalidatedAtTime": str(g.value(pds, PROV.invalidatedAtTime) or "") or None,
                    "status": _flags_of(g, node) if node is not None else None,
                    "progress": int(prog) if prog is not None else None,
                    "by": _label(g, by) if by is not None else None})
    sched = []
    for pds in g.objects(proc, IOC.hasSchedule):
        node = g.value(pds, IOC.hasScheduleValue)
        by = g.value(pds, IOC.generatedByActor)
        sched.append({"kind": "schedule", "pds": str(pds), "current": _is_current(g, pds),
                      "generatedAtTime": str(g.value(pds, PROV.generatedAtTime) or ""),
                      "invalidatedAtTime": str(g.value(pds, PROV.invalidatedAtTime) or "") or None,
                      "start": str(g.value(node, PROV.startedAtTime) or "")[:10] if node is not None else None,
                      "end": str(g.value(node, PROV.endedAtTime) or "")[:10] if node is not None else None,
                      "by": _label(g, by) if by is not None else None})
    out.sort(key=lambda x: x["generatedAtTime"], reverse=True)
    sched.sort(key=lambda x: x["generatedAtTime"], reverse=True)
    return {"process": _label(g, proc), "versions": out, "scheduleVersions": sched}

@app.post("/api/projects/{pid}/processes")
async def add_process(pid: str, req: AddProcessRequest):
    g, meta = _load_graph(pid), _load_meta(pid)
    pmark = req.id or f"P{uuid.uuid4().hex[:6]}"
    if _find_process(g, pmark) is not None:
        raise HTTPException(409, f"Process id '{pmark}' already exists")
    known = {str(g.value(p, IOC.hasInternalProcessMark) or ""): p for p in g.subjects(RDF.type, IOC.Process)}
    row = {"process_id": pmark, "name": req.label, "actor": req.actor or "", "actor_org": req.actorOrg or "",
           "method": req.method or "", "start_date": req.startDate or "", "end_date": req.endDate or "",
           "status": req.status, "progress": str(req.progress), "space": req.space or ""}
    rep = _empty_report("manual")
    g_new, _ = rows_to_graph([row], meta.get("name", pid), pid, rep)
    for t in g_new:
        if t[0] != URIRef(f"urn:ioc:project:{pid}"):
            g.add(t)
    proc = URIRef(f"{_base(pid)}{_slug(pmark)}")
    for ref in req.predecessors:
        target = URIRef(ref) if ref.startswith(("http", "urn")) else known.get(ref)
        if target is None or (target, RDF.type, IOC.Process) not in g:
            raise HTTPException(400, f"Predecessor '{ref}' not found")
        g.add((proc, IOC.hasPredecessor, target))
    _save_graph(pid, g)
    _save_meta(pid, meta.get("name", pid), len(_processes(g)), meta)
    return _extract_proc(g, proc)

@app.post("/api/projects/{pid}/processes/{proc_id}/status")
async def update_status(pid: str, proc_id: str, req: StatusUpdateRequest):
    g, meta = _load_graph(pid), _load_meta(pid)
    target = _find_process(g, proc_id)
    if target is None:
        raise HTTPException(404, "Process not found")
    changed = _write_status(g, pid, target, req.status, req.progress, req.by)
    if changed:
        _save_graph(pid, g)
        _save_meta(pid, meta.get("name", pid), len(_processes(g)), meta)
    return {"ok": True, "changed": changed, "process": _extract_proc(g, target)}

@app.delete("/api/projects/{pid}/processes/{proc_id}")
async def delete_process(pid: str, proc_id: str):
    g, meta = _load_graph(pid), _load_meta(pid)
    target = _find_process(g, proc_id)
    if target is None:
        raise HTTPException(404, "Process not found")
    prefix = str(target) + "_"
    for _, _, pds in list(g.triples((target, None, None))):
        if isinstance(pds, URIRef) and str(pds).startswith(prefix):
            for _, _, val in list(g.triples((pds, None, None))):
                if isinstance(val, URIRef) and str(val).startswith(prefix):
                    g.remove((val, None, None))
            g.remove((pds, None, None))
    g.remove((target, None, None))
    g.remove((None, None, target))
    _save_graph(pid, g)
    _save_meta(pid, meta.get("name", pid), len(_processes(g)), meta)
    return {"ok": True}

# ── Delay impact ────────────────────────────────────────────────────────────

@app.post("/api/projects/{pid}/impact")
async def analyse_impact(pid: str, req: ImpactRequest):
    """What-if: if process X finishes n days late, which processes, actors and the
    project end are affected? Read-only."""
    g = _load_graph(pid)
    proc = _find_process(g, req.process)
    if proc is None:
        raise HTTPException(404, "Process not found")
    net = _schedule_net(g)
    if proc not in net:
        raise HTTPException(400, "Summary processes cannot be delayed directly")
    return delay_impact(g, proc, _resolve_delay(net, proc, req))

@app.post("/api/projects/{pid}/impact/apply")
async def apply_impact(pid: str, req: ImpactRequest):
    """Re-plan: writes a new current ioc:Schedule state for the delayed process and every
    affected successor. Previous schedule states stay in the graph (baseline)."""
    g, meta = _load_graph(pid), _load_meta(pid)
    proc = _find_process(g, req.process)
    if proc is None:
        raise HTTPException(404, "Process not found")
    net = _schedule_net(g)
    if proc not in net:
        raise HTTPException(400, "Summary processes cannot be delayed directly")
    rep = delay_impact(g, proc, _resolve_delay(net, proc, req))
    for a in rep["affected"]:
        p = URIRef(a["uri"])
        if a["newStart"] and a["newEnd"]:
            _write_schedule(g, pid, p, date.fromisoformat(a["newStart"]), date.fromisoformat(a["newEnd"]),
                            req.by or "IoC Process Manager (delay propagation)")
    _save_graph(pid, g)
    _save_meta(pid, meta.get("name", pid), len(_processes(g)), meta)
    rep["applied"] = True
    return rep

@app.get("/api/projects/{pid}/delays")
async def get_current_delays(pid: str, request: Request):
    """Early warning: processes late at the reference date (?date=YYYY-MM-DD) and the
    combined knock-on effect on successors, actors and the project end."""
    g = _load_graph(pid)
    qd = request.query_params.get("date")
    ref = _d(qd) if qd else status_date_default(g)
    if ref is None:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    rep = current_delays(g, ref)
    rep["referenceDateSource"] = "query" if qd else "default (today, or day after the last finished process)"
    return rep

# ── Views ───────────────────────────────────────────────────────────────────

@app.get("/api/projects/{pid}/dependencies")
async def get_dependencies(pid: str):
    g = _load_graph(pid)
    node_map = {}
    for proc in _processes(g):
        d = _extract_proc(g, proc)
        node_map[d["uri"]] = {"id": d["uri"], "shortId": d["id"], "label": d["label"],
                              "actor": d["actor"]["label"] if d["actor"] else None, "status": d["status"],
                              "progress": d["progress"], "isRoot": not d["predecessors"], "isLeaf": not d["successors"]}
    edges = [{"source": str(pr), "target": str(p), "blocked": not node_map[str(pr)]["status"]["isFinished"]}
             for p in _processes(g) for pr in g.objects(p, IOC.hasPredecessor) if str(pr) in node_map]
    return {"nodes": list(node_map.values()), "edges": edges}

@app.get("/api/projects/{pid}/actors")
async def get_actors(pid: str):
    g, workload = _load_graph(pid), {}
    for proc in _processes(g):
        d = _extract_proc(g, proc)
        if d["actor"]:
            w = workload.setdefault(d["actor"]["label"], {"label": d["actor"]["label"], "uri": d["actor"]["uri"],
                                                          "processes": [], "total": 0, "finished": 0, "inProgress": 0})
            w["processes"].append(d); w["total"] += 1
            w["finished"] += d["status"]["isFinished"]
            w["inProgress"] += d["status"]["isStarted"] and not d["status"]["isFinished"]
    return {"actors": sorted(workload.values(), key=lambda x: x["label"])}

@app.get("/api/projects/{pid}/gantt")
async def get_gantt(pid: str):
    g, items = _load_graph(pid), []
    for proc in _processes(g):
        d = _extract_proc(g, proc)
        s = d["schedule"]
        if not s or not s["startedAtTime"] or not s["endedAtTime"]:
            continue
        start, end = s["startedAtTime"][:10], s["endedAtTime"][:10]
        try:
            days = (date.fromisoformat(end) - date.fromisoformat(start)).days or 1
        except ValueError:
            days = 1
        items.append({"id": d["uri"], "label": d["label"], "shortId": d["id"],
                      "actor": d["actor"]["label"] if d["actor"] else None, "startDate": start, "endDate": end,
                      "durationDays": days, "status": d["status"], "progress": d["progress"],
                      "predecessors": d["predecessors"], "parent": d["parent"]["label"] if d["parent"] else None,
                      "baselineEnd": s.get("baselineEnd"), "slipDays": s.get("slipDays", 0)})
    items.sort(key=lambda x: x["startDate"])
    return {"items": items, "minDate": min((i["startDate"] for i in items), default=None),
            "maxDate": max((i["endDate"] for i in items), default=None)}

# ── Validation ──────────────────────────────────────────────────────────────

@app.get("/api/projects/{pid}/validate")
async def validate_project(pid: str):
    return validate_abox(_load_graph(pid))

# ── SPARQL ──────────────────────────────────────────────────────────────────

@app.post("/api/projects/{pid}/sparql")
async def run_sparql(pid: str, req: SparqlQueryRequest):
    g = _load_graph(pid)
    if re.search(r"\b(INSERT|DELETE|LOAD|CLEAR|DROP|CREATE)\b", req.query, re.I):
        return {"success": False, "variables": [], "results": [], "count": 0,
                "error": "Only read queries are allowed here; use the status endpoint for updates."}
    try:
        result = g.query(req.query)
        variables = [str(v) for v in result.vars]
        rows = [{str(v): (str(row[v]) if row[v] is not None else "") for v in result.vars} for row in result]
        return {"success": True, "variables": variables, "results": rows, "count": len(rows)}
    except Exception as e:
        return {"success": False, "variables": [], "results": [], "count": 0, "error": str(e)}

PFX = "\n".join([
    "PREFIX ioc: <http://w3id.org/ioc#>", "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>",
    "PREFIX prov: <http://www.w3.org/ns/prov#>", "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>",
    "PREFIX ext: <urn:iocpm:ext#>"])
# current states only — the versioning pattern means there can be several states per property.
# (the type check is written as FILTER EXISTS so that rdflib's join planner does not build a cross product)
CUR_STATUS = "?process ioc:hasStatus ?spds . ?spds ioc:hasStatusValue ?st . FILTER EXISTS { ?spds a ioc:CurrentProcessDataState }"
CUR_SCHED = "?process ioc:hasSchedule ?schpds . ?schpds ioc:hasScheduleValue ?sched . FILTER EXISTS { ?schpds a ioc:CurrentProcessDataState }"
CUR_ACTOR = "?process ioc:hasActor ?apds . ?apds ioc:hasActorValue ?actor . ?actor rdfs:label ?actorLabel . FILTER EXISTS { ?apds a ioc:CurrentProcessDataState }"
WORK = "?process a ioc:Process ; rdfs:label ?label . FILTER NOT EXISTS { ?process ext:isSummary true }"

def _q(*body):
    return PFX + "\n\n" + "\n".join(body)

SPARQL_PRESETS = [
    # (id, category, requirement, title, description, query)
    ("q01", "Status & Progress", "FR4", "Open processes of one actor",
     "What are my current or next tasks? — edit the actor name",
     _q("SELECT ?label ?state WHERE {", f"  {WORK}", f"  {CUR_ACTOR}", f"  {CUR_STATUS}",
        '  ?st ioc:isFinished false ; ioc:isCancelled false .',
        '  BIND(IF(EXISTS{?st ioc:isStarted true},"started","not started") AS ?state)',
        '  FILTER(?actorLabel = "Maria Okonkwo")', "} ORDER BY ?label")),
    ("q02", "Status & Progress", "FR13", "Started, not finished",
     "isStarted = true ∧ isFinished = false",
     _q("SELECT ?process ?label WHERE {", f"  {WORK}", f"  {CUR_STATUS}",
        "  ?st ioc:isStarted true ; ioc:isFinished false .", "} ORDER BY ?label")),
    ("q03", "Status & Progress", "FR13", "Finished processes", "isFinished = true",
     _q("SELECT ?process ?label WHERE {", f"  {WORK}", f"  {CUR_STATUS}", "  ?st ioc:isFinished true .", "} ORDER BY ?label")),
    ("q04", "Status & Progress", "FR13", "Completion per actor", "Finished / total per actor",
     _q("SELECT ?actorLabel (COUNT(?process) AS ?total) (SUM(IF(?fin, 1, 0)) AS ?finished) WHERE {",
        f"  {WORK}", f"  {CUR_ACTOR}", f"  {CUR_STATUS}", "  ?st ioc:isFinished ?fin .",
        "} GROUP BY ?actorLabel ORDER BY ?actorLabel")),
    ("q05", "Dependencies", "FR5", "Can start next",
     "Not started, not cancelled, and every predecessor finished — 'What should I do next?'",
     _q("SELECT ?process ?label ?actorLabel WHERE {", f"  {WORK}", f"  {CUR_STATUS}",
        "  ?st ioc:isStarted false ; ioc:isCancelled false .",
        f"  OPTIONAL {{ {CUR_ACTOR} }}",
        "  FILTER NOT EXISTS {", "    ?process ioc:hasPredecessor ?pre .",
        "    ?pre ioc:hasStatus ?pp . ?pp ioc:hasStatusValue ?ps . ?ps ioc:isFinished false .",
        "    FILTER EXISTS { ?pp a ioc:CurrentProcessDataState }", "  }", "} ORDER BY ?label")),
    ("q06", "Dependencies", "FR6", "Blocked processes",
     "Not started and waiting for an unfinished predecessor",
     _q("SELECT ?label ?blockerLabel WHERE {", f"  {WORK}", f"  {CUR_STATUS}", "  ?st ioc:isStarted false .",
        "  ?process ioc:hasPredecessor ?blocker . ?blocker rdfs:label ?blockerLabel .",
        "  ?blocker ioc:hasStatus ?bp . ?bp ioc:hasStatusValue ?bs . ?bs ioc:isFinished false .",
        "  FILTER EXISTS { ?bp a ioc:CurrentProcessDataState }", "} ORDER BY ?label")),
    ("q07", "Dependencies", "FR7", "All (transitive) successors of a process",
     "What depends — directly or indirectly — on this process? Edit the label. Uses the property path hasPredecessor+",
     _q("SELECT DISTINCT ?label WHERE {", '  ?x rdfs:label "Drainage — Below Ground Foul & Surface Water" .',
        "  ?process ioc:hasPredecessor+ ?x ; rdfs:label ?label .", "} ORDER BY ?label")),
    ("q08", "Dependencies", "FR8", "Are the prerequisites of a process finished?",
     "Direct predecessors of one process with their current status",
     _q("SELECT ?predLabel ?finished WHERE {", '  ?p rdfs:label "Structural Screed Topping — Floors 1-3" ; ioc:hasPredecessor ?pre .',
        "  ?pre rdfs:label ?predLabel ; ioc:hasStatus ?pp . ?pp ioc:hasStatusValue ?ps . ?ps ioc:isFinished ?finished .",
        "  FILTER EXISTS { ?pp a ioc:CurrentProcessDataState }", "}")),
    ("q09", "Dependencies", "", "Direct predecessor relations", "All asserted ioc:hasPredecessor links",
     _q("SELECT ?label ?predLabel WHERE {", "  ?process a ioc:Process ; rdfs:label ?label ; ioc:hasPredecessor ?p .",
        "  ?p rdfs:label ?predLabel .", "} ORDER BY ?label")),
    ("q10", "Schedule", "FR9", "Overdue processes",
     "Planned end before the reference date but not finished — edit ?today",
     _q("SELECT ?label ?end ?actorLabel WHERE {", '  BIND("2025-10-01T00:00:00"^^xsd:dateTime AS ?today)',
        f"  {WORK}", f"  {CUR_SCHED}", "  ?sched prov:endedAtTime ?end .", f"  {CUR_STATUS}",
        "  ?st ioc:isFinished false ; ioc:isCancelled false .", f"  OPTIONAL {{ {CUR_ACTOR} }}",
        "  FILTER(?end < ?today)", "} ORDER BY ?end")),
    ("q11", "Schedule", "", "Processes by start date", "prov:startedAtTime / prov:endedAtTime",
     _q("SELECT ?label ?start ?end WHERE {", f"  {WORK}", f"  {CUR_SCHED}", "  ?sched prov:startedAtTime ?start .",
        "  OPTIONAL { ?sched prov:endedAtTime ?end }", "} ORDER BY ?start")),
    ("q12", "Schedule", "", "Processes without schedule", "Data-quality check",
     _q("SELECT ?process ?label WHERE {", f"  {WORK}", "  FILTER NOT EXISTS { ?process ioc:hasSchedule ?s }", "}")),
    ("q13", "Resources & Materials", "FR10", "Materials needed in a period",
     "Raw material inputs of processes starting in a time window — edit the dates",
     _q("SELECT ?start ?label ?material WHERE {", f"  {WORK}", f"  {CUR_SCHED}", "  ?sched prov:startedAtTime ?start .",
        "  ?process ioc:hasInputElement ?e . ?e ioc:hasInputElementValue ?m . ?m a ioc:RawMaterial ; rdfs:label ?material .",
        '  FILTER(?start >= "2025-09-01T00:00:00"^^xsd:dateTime && ?start < "2025-11-01T00:00:00"^^xsd:dateTime)',
        "} ORDER BY ?start")),
    ("q14", "Resources & Materials", "", "Resources and where they are used", "ioc:Resource per process",
     _q("SELECT ?resource ?label WHERE {", f"  {WORK}",
        "  ?process ioc:hasResource ?r . ?r ioc:hasResourceValue ?res . ?res rdfs:label ?resource .", "} ORDER BY ?resource")),
    ("q15", "Resources & Materials", "", "Output elements", "ioc:Element produced by processes",
     _q("SELECT ?label ?element WHERE {", f"  {WORK}",
        "  ?process ioc:hasOutputElement ?o . ?o ioc:hasOutputElementValue ?el . ?el rdfs:label ?element .", "}")),
    ("q16", "Space & Structure", "FR12", "Processes per location", "ioc:hasInitialSpace → ioc:Location",
     _q("SELECT ?space (COUNT(?process) AS ?n) (GROUP_CONCAT(?label; separator=' | ') AS ?processes) WHERE {", f"  {WORK}",
        "  ?process ioc:hasInitialSpace ?sp . ?sp ioc:hasInitialSpaceValue ?loc . ?loc rdfs:label ?space .",
        "} GROUP BY ?space ORDER BY DESC(?n)")),
    ("q17", "Space & Structure", "FR13", "Completion per phase",
     "Children of summary processes (ioc:hasParent) — available for XLSX/MS Project imports",
     _q("SELECT ?phase (COUNT(?process) AS ?total) (SUM(IF(?fin,1,0)) AS ?finished) WHERE {",
        "  ?process ioc:hasParent ?ph . ?ph rdfs:label ?phase .", f"  {CUR_STATUS}", "  ?st ioc:isFinished ?fin .",
        "} GROUP BY ?phase ORDER BY ?phase")),
    ("q18", "Provenance", "FR11", "Status history of a process",
     "All ProcessDataState versions — the previous states are kept, not overwritten",
     _q("SELECT ?generated ?invalidated ?current ?started ?finished ?paused ?progress ?by WHERE {",
        '  ?process rdfs:label "Thermal & Acoustic Insulation — Floors & Partitions" ; ioc:hasStatus ?pds .',
        "  ?pds prov:generatedAtTime ?generated ; ioc:hasStatusValue ?st .",
        "  ?st ioc:isStarted ?started ; ioc:isFinished ?finished ; ioc:isPaused ?paused .",
        "  OPTIONAL { ?st ext:progress ?progress }", "  OPTIONAL { ?pds prov:invalidatedAtTime ?invalidated }",
        "  OPTIONAL { ?pds ioc:generatedByActor/rdfs:label ?by }",
        "  BIND(EXISTS { ?pds a ioc:CurrentProcessDataState } AS ?current)", "} ORDER BY DESC(?generated)")),
    ("q19", "Provenance", "", "Re-planned processes (schedule slip)",
     "Processes whose current schedule differs from the baseline — result of applied delay propagation",
     _q("SELECT ?label ?baselineEnd ?currentEnd ?by WHERE {", f"  {WORK}",
        "  ?process ioc:hasSchedule ?old . ?old ioc:hasScheduleValue ?os . ?os prov:endedAtTime ?baselineEnd .",
        "  FILTER NOT EXISTS { ?old prov:wasRevisionOf ?any }",
        f"  {CUR_SCHED}", "  ?sched prov:endedAtTime ?currentEnd .",
        "  OPTIONAL { ?schpds ioc:generatedByActor/rdfs:label ?by }",
        "  FILTER(?currentEnd != ?baselineEnd)", "} ORDER BY ?baselineEnd")),
    ("q20", "Ontology", "", "ProcessDataState chain of one process",
     "Shows the IoC versioning pattern: Process → PDS → value",
     _q("SELECT ?prop ?pds ?value ?valueLabel WHERE {", '  ?process rdfs:label "Topographic & Boundary Survey" ; ?prop ?pds .',
        "  ?pds ?vp ?value . FILTER EXISTS { ?pds a ioc:CurrentProcessDataState }",
        "  FILTER(?vp != rdf:type && ?vp != prov:generatedAtTime)",
        "  OPTIONAL { ?value rdfs:label ?valueLabel }", "} ORDER BY ?prop").replace(
        "PREFIX ext:", "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\nPREFIX ext:")),
    ("q21", "Ontology", "", "Instances per IoC class", "Asserted rdf:type counts",
     _q("SELECT ?class (COUNT(DISTINCT ?x) AS ?n) WHERE {", "  ?x a ?class .",
        '  FILTER(STRSTARTS(STR(?class), "http://w3id.org/ioc#"))', "} GROUP BY ?class ORDER BY DESC(?n)")),
]

@app.get("/api/sparql/presets")
async def get_sparql_presets():
    return {"presets": [{"id": i, "category": c, "fr": fr, "title": t, "description": d, "query": q}
                        for i, c, fr, t, d, q in SPARQL_PRESETS]}

# ── Ontology browser (derived from the TBox at run time) ────────────────────

@app.get("/api/tbox")
async def get_tbox_summary():
    return _tbox_description()

def _tbox_description() -> dict:
    def lit(node, prop):
        v = TBOX.value(node, prop)
        return str(v) if v is not None else ""
    classes = []
    for c in sorted(TBOX_CLASSES, key=str):
        parents = [p for p in TBOX.objects(c, RDFS.subClassOf) if isinstance(p, URIRef)]
        is_ioc = str(c).startswith(str(IOC))
        if not is_ioc and not any(str(p).startswith(str(IOC)) for p in parents):
            continue                                  # skip unrelated external declarations
        classes.append({"name": _qname(c).split(":", 1)[-1] if is_ioc else _qname(c), "qname": _qname(c),
                        "uri": str(c), "external": not is_ioc, "label": lit(c, RDFS.label),
                        "comment": lit(c, RDFS.comment), "parents": [_qname(p) for p in parents],
                        "disjointWith": sorted(_qname(o) for o in TBOX.objects(c, OWL.disjointWith))})
    props = []
    for p in sorted(TBOX_OBJ_PROPS | TBOX_DAT_PROPS, key=str):
        chars = [n for n, t in (("functional", OWL.FunctionalProperty), ("transitive", OWL.TransitiveProperty))
                 if (p, RDF.type, t) in TBOX]
        inv = TBOX.value(p, OWL.inverseOf)
        if inv is None:
            inv = next(TBOX.subjects(OWL.inverseOf, p), None)
        props.append({"qname": _qname(p), "uri": str(p),
                      "kind": "object" if p in TBOX_OBJ_PROPS else "datatype",
                      "domain": [_qname(c) for d in TBOX.objects(p, RDFS.domain) for c in _named_classes_of(d)],
                      "range": [_qname(c) for r in TBOX.objects(p, RDFS.range) for c in (_named_classes_of(r) or [r])],
                      "superProperty": [_qname(s) for s in TBOX.objects(p, RDFS.subPropertyOf)],
                      "characteristics": chars, "inverseOf": _qname(inv) if inv is not None else None,
                      "comment": lit(p, RDFS.comment)})
    return {"version": TBOX_VERSION, "namespace": str(IOC), "classes": classes, "properties": props}

@app.get("/api/projects/{pid}/ontology")
async def get_ontology(pid: str):
    g, tb = _load_graph(pid), _tbox_description()
    by_class = {}
    for s, o in g.subject_objects(RDF.type):
        for sup in SUPER.get(o, {o}):
            by_class.setdefault(sup, set()).add(s)
    for c in tb["classes"]:
        insts = by_class.get(URIRef(c["uri"]), set())
        c["instanceCount"] = len(insts)
        c["directCount"] = sum(1 for _ in g.subjects(RDF.type, URIRef(c["uri"])))
        sample = []
        for inst in sorted(insts, key=str)[:300]:
            item = {"uri": str(inst), "label": _label(g, inst)}
            if (inst, RDF.type, IOC.Process) in g:
                item["status"] = _read_status(g, inst)
                a = _current_value(g, inst, IOC.hasActor, IOC.hasActorValue)
                if a is not None:
                    item["actor"] = _label(g, a)
            sample.append(item)
        c["instances"] = sample
    used = {p for _, p, _ in g}
    for p in tb["properties"]:
        p["usedInProject"] = URIRef(p["uri"]) in used
    tb["extensionTerms"] = sorted({_qname(p) for p in used if str(p).startswith(str(EXT))} |
                                  {_qname(o) for o in g.objects(None, RDF.type) if str(o).startswith(str(EXT))})
    return tb

# ── RDF export ──────────────────────────────────────────────────────────────

@app.get("/api/projects/{pid}/rdf")
async def get_rdf(pid: str):
    _check_pid(pid)
    path = _project_path(pid)
    if not path.exists():
        raise HTTPException(404, "Project not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/turtle")

# ── Import ──────────────────────────────────────────────────────────────────

def _import(rows, report, name, pid):
    g, count = rows_to_graph(rows, name, pid, report)
    report["validation"] = {k: v for k, v in validate_abox(g, max_issues=20).items() if k != "issues"}
    _save_graph(pid, g)
    _save_meta(pid, name, count, format=report["format"])
    return {"id": pid, "name": name, "processes": count, "warnings": report["warnings"], "report": report}

@app.post("/api/import/csv")
async def import_csv(req: Request):
    body = await req.json()
    rows, report = parse_csv(body.get("csv", ""))
    if not rows:
        return {"error": "CSV is empty or has no recognisable columns"}
    return _import(rows, report, body.get("name", "Imported Project"), "proj_" + uuid.uuid4().hex[:8])

@app.post("/api/import/file")
async def import_file(req: Request):
    """Body: {name, filename, data (base64)} — CSV, XLSX or MS Project XML (MSPDI)."""
    body = await req.json()
    try:
        data = base64.b64decode(body.get("data", ""))
        rows, report = detect_and_parse(body.get("filename", ""), data)
    except Exception as e:
        return {"error": f"Could not read file: {e}"}
    if not rows:
        return {"error": "No processes found in file", "report": report}
    return _import(rows, report, body.get("name", "Imported Project"), "proj_" + uuid.uuid4().hex[:8])

@app.get("/api/template/csv")
async def get_csv_template():
    template = (
        "process_id,name,method,actor,actor_org,start_date,end_date,"
        "predecessors,resources,input_materials,output_elements,space,status,progress,notes\n"
        "P01,Site Survey,Total Station Survey,Jane Smith,Survey Co,2025-09-01,2025-09-03,,Total Station;GPS,,Survey Report,Site,finished,100,Initial boundary survey\n"
        "P02,Excavation,Open-cut Excavation,James Lee,Groundworks Ltd,2025-09-04,2025-09-10,P01,360 Excavator,,,Zone A,finished,100,\n"
        "P03,Rebar Fixing,Rebar Cage Fixing,Kofi A.,Steel Fix Ltd,2025-09-11,2025-09-15,P02,,B500 16mm Rebar,,Zone A,started,60,\n"
        "P04,Concrete Pour,Ready-mix Pouring,Tom B.,Concrete Crew,2025-09-16,2025-09-16,P03,Concrete Pump,C30/37 Ready-mix,Strip Foundation,Zone A,planned,0,\n")
    return PlainTextResponse(template, media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="ioc_template.csv"'})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
