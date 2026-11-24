"""Build the knowledge graph into Neo4j, mirrored in Postgres.

Sources:
  * machines, parts and suppliers come from the database (authoritative)
  * Deviation and SOP facts are extracted from the documents

Document extraction runs two ways on purpose. A deterministic header parse of
the QDR / SOP front matter is the authority, because those headers are
structured and a 3B model paraphrasing 'Supplier: Arcflow Castings' into
'Arcflow' would fragment the graph. An LLM pass restricted to the schema runs
alongside it and may only *add* triples whose entities already exist - it
cannot invent a machine or rename a supplier. Both paths record which documents
a fact came from.

Every node and edge carries source_doc_ids, which is what lets the query layer
drop a path the asking user could not read for themselves.

    python -m app.graph.build
"""
from __future__ import annotations

import json
import re
import sys

from app.config import settings
from app.db import admin_conn
from app.graph.resolve import (
    RELATIONSHIPS,
    Resolver,
    expand_machines,
    line_of_machine,
    node_key,
    normalise,
)
from app.llm import extract_json, generate

PROMPT_VERSION = "graph-extract-v1.3.0"

EXTRACT_SYSTEM = """You extract facts from a manufacturing document into a fixed \
graph schema. Reply with JSON only.

Allowed node labels: Machine, Part, Supplier, Deviation, SOP
Allowed relationships (and their direction):
  Machine -RUNS-> Part
  Part -SUPPLIED_BY-> Supplier
  Deviation -FOUND_ON-> Machine
  Deviation -INVOLVES-> Part
  SOP -COVERS-> Machine

Rules:
- Use ONLY these labels and relationship names.
- Copy identifiers exactly: machine ids look like CNC-L1-04, part numbers like
  HX-220-B, deviation ids like QDR-2026-014, SOP ids like SOP-L3-007.
- Use the supplier's full name as written in the document.
- Do not invent entities. If the document does not state a fact, omit it.
- Reply with this shape and nothing else:
{"triples":[{"src_label":"Deviation","src":"QDR-2026-014","rel":"FOUND_ON","dst_label":"Machine","dst":"CNC-L1-04"}]}"""


# -------------------------------------------------------------- deterministic

_HEADER = re.compile(r"^\s*([A-Za-z][A-Za-z ]{1,24})\s*:\s*(.+?)\s*$", re.M)


def parse_headers(text: str) -> dict[str, str]:
    """Key: value front matter, as written in the QDR and SOP templates."""
    out: dict[str, str] = {}
    for key, val in _HEADER.findall(text or ""):
        k = key.strip().lower()
        if k not in out:
            out[k] = val.strip()
    # Markdown tables carry the same fields as | Field | Value | rows.
    for line in (text or "").splitlines():
        if line.count("|") >= 2:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[0] and cells[1]:
                k = cells[0].lower()
                if k not in out:
                    out[k] = cells[1]
    return out


def doc_text(doc_id: str) -> str:
    with admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT text FROM doc_chunks WHERE doc_id = %s ORDER BY ord", (doc_id,)
        )
        return "\n\n".join(r[0] for r in cur.fetchall())


def collect_deterministic(resolver: Resolver, known_machines: set[str],
                          known_parts: set[str]) -> list[dict]:
    """Deviation and SOP triples from structured document headers."""
    triples: list[dict] = []

    with admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT doc_id, title, version, effective_date FROM documents"
            " WHERE doc_id LIKE 'QDR-%' OR doc_id LIKE 'SOP-%' OR doc_id LIKE 'WI-%'"
            " ORDER BY doc_id"
        )
        docs = cur.fetchall()

    for doc_id, title, version, eff in docs:
        text = doc_text(doc_id)
        if not text:
            continue
        hdr = parse_headers(text)

        if doc_id.startswith("QDR-"):
            machine = (hdr.get("machine") or "").strip()
            part = (hdr.get("part") or "").strip()
            supplier = (hdr.get("supplier") or "").strip()
            line = (hdr.get("line") or "").strip()
            date = (hdr.get("date") or (eff.isoformat() if eff else "")).strip()

            resolver.add("Deviation", doc_id)
            triples.append({
                "src_label": "Deviation", "src": doc_id, "rel": "_PROPS",
                "props": {
                    "line": line or (line_of_machine(machine) or ""),
                    "date": date,
                    "status": hdr.get("status", ""),
                    "classification": hdr.get("classification", ""),
                    "supplier": supplier,
                },
                "source_doc_ids": [doc_id],
            })

            if machine in known_machines:
                triples.append({
                    "src_label": "Deviation", "src": doc_id, "rel": "FOUND_ON",
                    "dst_label": "Machine", "dst": machine,
                    "source_doc_ids": [doc_id],
                })
            if part in known_parts:
                triples.append({
                    "src_label": "Deviation", "src": doc_id, "rel": "INVOLVES",
                    "dst_label": "Part", "dst": part,
                    "source_doc_ids": [doc_id],
                })
            if supplier and part in known_parts:
                # The deviation itself is evidence of this supply relationship.
                triples.append({
                    "src_label": "Part", "src": part, "rel": "SUPPLIED_BY",
                    "dst_label": "Supplier", "dst": supplier,
                    "source_doc_ids": [doc_id],
                })

        else:  # SOP / WI: which machines does it cover?
            applies = hdr.get("applies to") or hdr.get("equipment") or ""
            machines = expand_machines(applies, known_machines)
            if not machines:
                machines = expand_machines(text[:1200], known_machines)
            for mid in machines:
                triples.append({
                    "src_label": "SOP", "src": doc_id, "rel": "COVERS",
                    "dst_label": "Machine", "dst": mid,
                    "source_doc_ids": [doc_id],
                })
            if machines:
                triples.append({
                    "src_label": "SOP", "src": doc_id, "rel": "_PROPS",
                    "props": {"title": title, "version": version or ""},
                    "source_doc_ids": [doc_id],
                })

    return triples


# ------------------------------------------------------------------ LLM pass

def collect_llm(known_machines: set[str], known_parts: set[str],
                known_suppliers: set[str], doc_ids: list[str]) -> list[dict]:
    """LLM-extracted triples, admitted only if every entity already exists."""
    out: list[dict] = []
    allowed_rels = set(RELATIONSHIPS)

    for doc_id in doc_ids:
        text = doc_text(doc_id)
        if not text:
            continue
        try:
            res = generate(
                prompt=f"Document {doc_id}:\n\n{text[:4000]}\n\nJSON:",
                system=EXTRACT_SYSTEM,
                model=settings.answer_model,
                json_mode=True,
                max_tokens=600,
            )
        except Exception:
            continue

        data = extract_json(res.text) or {}
        for t in data.get("triples", []) or []:
            if not isinstance(t, dict):
                continue
            rel = str(t.get("rel", "")).upper().strip()
            if rel not in allowed_rels:
                continue
            src_label, dst_label = RELATIONSHIPS[rel]
            if str(t.get("src_label", src_label)) != src_label:
                continue
            if str(t.get("dst_label", dst_label)) != dst_label:
                continue
            src, dst = str(t.get("src", "")).strip(), str(t.get("dst", "")).strip()
            if not src or not dst:
                continue

            # Containment: the model may connect existing things, never create.
            if dst_label == "Machine" and dst not in known_machines:
                continue
            if dst_label == "Part" and dst not in known_parts:
                continue
            if dst_label == "Supplier" and normalise(dst) not in known_suppliers:
                continue
            if src_label == "Machine" and src not in known_machines:
                continue
            if src_label == "Part" and src not in known_parts:
                continue

            out.append({
                "src_label": src_label, "src": src, "rel": rel,
                "dst_label": dst_label, "dst": dst,
                "source_doc_ids": [doc_id], "via_llm": True,
            })
    return out


# ------------------------------------------------------------------- building

def build(use_llm: bool = True) -> dict:
    resolver = Resolver()
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def add_node(label: str, name: str, props: dict | None = None, srcs=()):
        key = resolver.add(label, name)
        n = nodes.setdefault(key, {
            "node_key": key, "label": label, "name": name,
            "props": {}, "source_doc_ids": set(),
        })
        if props:
            n["props"].update({k: v for k, v in props.items() if v not in (None, "")})
        n["source_doc_ids"].update(srcs)
        return key

    def add_edge(sl, s, rel, dl, d, srcs=(), props=None):
        sk = add_node(sl, s)
        dk = add_node(dl, d)
        k = (sk, rel, dk)
        e = edges.setdefault(k, {
            "src": sk, "rel": rel, "dst": dk, "props": {},
            "source_doc_ids": set(),
        })
        if props:
            e["props"].update(props)
        e["source_doc_ids"].update(srcs)
        return k

    # ---- 1. structural facts from the database
    with admin_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine_id, line, type, model FROM machines ORDER BY machine_id")
        machine_rows = cur.fetchall()
        for mid, line, mtype, model in machine_rows:
            add_node("Machine", mid, {"line": line, "type": mtype, "model": model})

        cur.execute(
            "SELECT DISTINCT machine_id, part_no FROM work_orders ORDER BY machine_id, part_no"
        )
        for mid, part in cur.fetchall():
            add_node("Part", part)
            add_edge("Machine", mid, "RUNS", "Part", part)

        cur.execute(
            "SELECT part_no, supplier, valid_from FROM supplier_parts"
            " ORDER BY part_no, supplier"
        )
        for part, supplier, valid_from in cur.fetchall():
            add_node("Part", part)
            add_node("Supplier", supplier)
            # Deliberately no price on the graph: the graph is readable by roles
            # that may not see unit_price_inr.
            add_edge("Part", part, "SUPPLIED_BY", "Supplier", supplier,
                     props={"valid_from": str(valid_from)})

    known_machines = {m[0] for m in machine_rows}
    known_parts = {k.split(":", 1)[1] for k in nodes if k.startswith("Part:")}
    known_parts = {nodes[k]["name"] for k in nodes if k.startswith("Part:")}
    known_suppliers = {normalise(nodes[k]["name"]) for k in nodes if k.startswith("Supplier:")}

    # ---- 2. document facts, deterministic
    det = collect_deterministic(resolver, known_machines, known_parts)
    for t in det:
        if t["rel"] == "_PROPS":
            add_node(t["src_label"], t["src"], t.get("props"), t.get("source_doc_ids", []))
            continue
        add_edge(t["src_label"], t["src"], t["rel"], t["dst_label"], t["dst"],
                 t.get("source_doc_ids", []))
        # The deviation node itself is sourced from its document.
        if t["src_label"] == "Deviation":
            add_node("Deviation", t["src"], None, t.get("source_doc_ids", []))
        if t["src_label"] == "SOP":
            add_node("SOP", t["src"], None, t.get("source_doc_ids", []))

    # ---- 3. document facts, LLM, constrained to existing entities
    llm_added = 0
    if use_llm:
        with admin_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT doc_id FROM documents WHERE doc_id LIKE 'QDR-%'"
                " OR doc_id IN ('ASL-2026','SQA-ARC-01','SQA-MTS-01') ORDER BY doc_id"
            )
            llm_docs = [r[0] for r in cur.fetchall()]
        for t in collect_llm(known_machines, known_parts, known_suppliers, llm_docs):
            before = len(edges)
            add_edge(t["src_label"], t["src"], t["rel"], t["dst_label"], t["dst"],
                     t.get("source_doc_ids", []))
            if len(edges) > before:
                llm_added += 1

    # ---- 4. canonical display names
    for key, n in nodes.items():
        n["name"] = resolver.canonical(n["label"], n["name"])
        n["source_doc_ids"] = sorted(n["source_doc_ids"])
    for e in edges.values():
        e["source_doc_ids"] = sorted(e["source_doc_ids"])

    # Suppliers and parts known only structurally have no document source; a
    # path through them must not be dropped for lack of a citation, so they
    # are marked as sourced from the database rather than a document.
    for n in nodes.values():
        if not n["source_doc_ids"]:
            n["props"].setdefault("origin", "database")

    write_postgres(nodes, edges)
    neo = write_neo4j(nodes, edges)

    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "llm_edges_added": llm_added,
        "neo4j": neo,
        "by_label": {
            lbl: sum(1 for n in nodes.values() if n["label"] == lbl)
            for lbl in ("Machine", "Part", "Supplier", "Deviation", "SOP")
        },
        "by_rel": {
            rel: sum(1 for e in edges.values() if e["rel"] == rel)
            for rel in RELATIONSHIPS
        },
    }


def write_postgres(nodes: dict, edges: dict) -> None:
    with admin_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE graph_edges, graph_nodes CASCADE")
            cur.executemany(
                "INSERT INTO graph_nodes (node_key, label, name, props, source_doc_ids)"
                " VALUES (%s,%s,%s,%s,%s)",
                [
                    (n["node_key"], n["label"], n["name"],
                     json.dumps(n["props"]), n["source_doc_ids"])
                    for n in nodes.values()
                ],
            )
            cur.executemany(
                "INSERT INTO graph_edges (src, rel, dst, props, source_doc_ids)"
                " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (src, rel, dst) DO NOTHING",
                [
                    (e["src"], e["rel"], e["dst"], json.dumps(e["props"]),
                     e["source_doc_ids"])
                    for e in edges.values()
                ],
            )
        conn.commit()


def write_neo4j(nodes: dict, edges: dict) -> dict:
    """Load into Neo4j. Returns a status dict; never raises."""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return {"ok": False, "reason": "neo4j driver not installed"}

    try:
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        driver.verify_connectivity()
    except Exception as exc:
        return {"ok": False, "reason": f"neo4j unreachable: {str(exc)[:160]}"}

    try:
        with driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n")
            s.run(
                "CREATE CONSTRAINT node_key_unique IF NOT EXISTS "
                "FOR (n:Entity) REQUIRE n.node_key IS UNIQUE"
            )
            for n in nodes.values():
                s.run(
                    f"MERGE (x:Entity:{n['label']} {{node_key: $key}}) "
                    "SET x.name = $name, x.label = $label, "
                    "    x.source_doc_ids = $srcs, x.props = $props",
                    key=n["node_key"], name=n["name"], label=n["label"],
                    srcs=n["source_doc_ids"], props=json.dumps(n["props"]),
                )
            for e in edges.values():
                s.run(
                    "MATCH (a:Entity {node_key: $src}), (b:Entity {node_key: $dst}) "
                    f"MERGE (a)-[r:{e['rel']}]->(b) "
                    "SET r.source_doc_ids = $srcs, r.props = $props",
                    src=e["src"], dst=e["dst"], srcs=e["source_doc_ids"],
                    props=json.dumps(e["props"]),
                )
            counts = s.run(
                "MATCH (n) WITH count(n) AS nodes "
                "MATCH ()-[r]->() RETURN nodes, count(r) AS rels"
            ).single()
        driver.close()
        return {"ok": True, "nodes": counts["nodes"], "rels": counts["rels"]}
    except Exception as exc:
        try:
            driver.close()
        except Exception:
            pass
        return {"ok": False, "reason": str(exc)[:200]}


def main() -> int:
    no_llm = "--no-llm" in sys.argv
    print("building graph ...")
    stats = build(use_llm=not no_llm)
    print(f"\nnodes {stats['nodes']}  edges {stats['edges']}"
          f"  (LLM contributed {stats['llm_edges_added']} new edges)")
    print("  by label:", ", ".join(f"{k}={v}" for k, v in stats["by_label"].items()))
    print("  by rel  :", ", ".join(f"{k}={v}" for k, v in stats["by_rel"].items()))
    neo = stats["neo4j"]
    if neo.get("ok"):
        print(f"  neo4j   : loaded {neo['nodes']} nodes, {neo['rels']} relationships")
    else:
        print(f"  neo4j   : NOT loaded - {neo.get('reason')}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
