"""
make_mspdi_fixture.py — derives an MS Project XML (MSPDI) file from the
Thornfield Court XLSX so that the same synthetic project exists in three
source formats (CSV, XLSX, MSPDI) for the data-unification evaluation.

It deliberately writes only what an MS Project schedule natively holds:
tasks, phases (summary tasks), dates, % complete, FS links, notes and
resource assignments. Method, space and output elements are NOT written,
because MS Project has no native field for them.

Usage:  python tools/make_mspdi_fixture.py sample_data/thornfield_court_sample_data.xlsx sample_data/thornfield_court.xml
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server_fastapi import parse_xlsx  # noqa: E402

NS = "http://schemas.microsoft.com/project"


def build(xlsx_path: str, out_path: str):
    rows, _ = parse_xlsx(Path(xlsx_path).read_bytes())
    ET.register_namespace("", NS)
    proj = ET.Element(f"{{{NS}}}Project")
    def sub(parent, tag, text=None):
        e = ET.SubElement(parent, f"{{{NS}}}{tag}")
        if text is not None:
            e.text = str(text)
        return e
    sub(proj, "Name", "Thornfield Court Residential Development")
    sub(proj, "ScheduleFromStart", "1")
    tasks, resources, assignments = sub(proj, "Tasks"), sub(proj, "Resources"), sub(proj, "Assignments")

    res_uid = {}
    def resource(name, rtype, group):
        if name not in res_uid:
            res_uid[name] = len(res_uid) + 1
            r = sub(resources, "Resource")
            sub(r, "UID", res_uid[name]); sub(r, "ID", res_uid[name]); sub(r, "Name", name)
            sub(r, "Type", rtype)                 # 1 = work, 0 = material
            sub(r, "Group", group)
        return res_uid[name]

    uid_of, outline, phase_no, child_no = {}, {}, 0, {}
    for i, row in enumerate(rows, start=1):
        uid_of[row["process_id"]] = i
    for i, row in enumerate(rows, start=1):
        t = sub(tasks, "Task")
        sub(t, "UID", i); sub(t, "ID", i); sub(t, "Name", row["name"])
        if row.get("_summary"):
            phase_no += 1
            outline[row["process_id"]] = str(phase_no)
            sub(t, "OutlineNumber", phase_no); sub(t, "OutlineLevel", 1); sub(t, "Summary", "1")
            continue
        par = outline.get(row.get("parent"), "0")
        child_no[par] = child_no.get(par, 0) + 1
        sub(t, "OutlineNumber", f"{par}.{child_no[par]}"); sub(t, "OutlineLevel", 2); sub(t, "Summary", "0")
        sub(t, "Start", f"{row['start_date'][:10]}T08:00:00")
        sub(t, "Finish", f"{row['end_date'][:10]}T17:00:00")
        pct = int(float(row.get("progress") or 0))
        sub(t, "PercentComplete", pct)
        if row.get("status") in ("started", "finished"):
            sub(t, "ActualStart", f"{row['start_date'][:10]}T08:00:00")
        if row.get("status") == "finished":
            sub(t, "ActualFinish", f"{row['end_date'][:10]}T17:00:00")
        if row.get("notes"):
            sub(t, "Notes", row["notes"])
        for pred in row.get("predecessors") or []:
            link = sub(t, "PredecessorLink")
            sub(link, "PredecessorUID", uid_of[pred]); sub(link, "Type", 1); sub(link, "LinkLag", 0)
        assigned = []
        if row.get("actor"):
            assigned.append(resource(row["actor"], 1, row.get("actor_org", "")))
        assigned += [resource(r, 1, "Equipment") for r in row.get("resources") or []]
        assigned += [resource(m, 0, "Material") for m in row.get("input_materials") or []]
        for ruid in assigned:
            a = sub(assignments, "Assignment")
            sub(a, "TaskUID", i); sub(a, "ResourceUID", ruid)
    ET.indent(proj)
    ET.ElementTree(proj).write(out_path, encoding="utf-8", xml_declaration=True)
    print(f"wrote {out_path}: {len(rows)} tasks, {len(res_uid)} resources")


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
