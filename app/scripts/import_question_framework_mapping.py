"""
import_question_framework_mapping.py
-------------------------------------
Idempotent seed script: reads Question_Framework_Mapping.xlsx and creates/merges:
  - canonical `question` nodes
  - `framework` nodes for each supported standard
  - `control` nodes for each framework clause (only if they don't already exist)
  - `control -[:HAS_QUESTION]-> question` relationships

Run from the project root:
    python app/scripts/import_question_framework_mapping.py [--dry-run] [--xlsx PATH]

Environment variables (or .env file):
    NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
"""

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from neo4j import AsyncGraphDatabase

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FRAMEWORK_COLUMNS = [
    ("PISF",         "D", "E"),
    ("NIST CSF 2.0", "F", "G"),
    ("SBP ETGRMF",   "H", "I"),
    ("NEPRA",        "J", "K"),
    ("CIS v8",       "L", "M"),
    ("PCI DSS v4.0", "N", "O"),
    ("PTA CTDISR",   "P", "Q"),
    ("SOC 2",        "R", "S"),
]

ISO_FRAMEWORK_NAME = "ISO 27001"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(text: str) -> str:
    """Lower-case + collapse whitespace for stable dedup key."""
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _parse_ids(raw: str) -> list[str]:
    """
    Split a comma-separated ID string, also handle ranges like 'P1.0-P8.0'
    by keeping them as-is (they represent a single reference).
    Returns list of stripped non-empty strings.
    """
    if not raw or not str(raw).strip():
        return []
    raw = str(raw).strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts


def _load_excel(xlsx_path: str) -> pd.DataFrame:
    df = pd.read_excel(xlsx_path, header=0)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.fillna("")
    return df


# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------

async def _merge_framework(session, name: str, dry_run: bool) -> str:
    """Ensure a framework node exists and return its id property."""
    fid = f"framework-{re.sub(r'[^a-z0-9]', '-', name.lower())}"
    if not dry_run:
        await session.run(
            """
            MERGE (f:framework {framework_name: $name})
            ON CREATE SET f.id = $fid, f.created_at = $now, f.updated_at = $now
            ON MATCH SET  f.updated_at = $now
            """,
            name=name, fid=fid, now=_now_iso(),
        )
    return fid


async def _merge_control(
    session, framework_name: str, control_id_val: str,
    control_name: str, framework_node_id: str, dry_run: bool
) -> str:
    """Ensure a control node exists and is owned by its framework. Returns node id."""
    node_id = f"control-{re.sub(r'[^a-z0-9]', '-', (framework_name + '-' + control_id_val).lower())}"
    if not dry_run:
        await session.run(
            """
            MERGE (c:control {control_id: $cid, framework: $fw_name})
            ON CREATE SET
                c.id           = $node_id,
                c.control_name = $control_name,
                c.created_at   = $now,
                c.updated_at   = $now
            ON MATCH SET
                c.updated_at   = $now
            WITH c
            MATCH (f:framework {framework_name: $fw_name})
            MERGE (f)-[:owns]->(c)
            """,
            cid=control_id_val, fw_name=framework_name,
            node_id=node_id, control_name=control_name, now=_now_iso(),
        )
    return node_id


async def _merge_question(
    session, text: str, weight: float, iso_control_id: str, dry_run: bool
) -> str:
    """Ensure a canonical question node exists (keyed by iso_control_id + normalized text)."""
    key = f"{iso_control_id}||{_normalize_text(text)}"
    if not dry_run:
        result = await session.run(
            """
            MERGE (q:question {dedup_key: $key})
            ON CREATE SET
                q.id             = 'question-' + toString(id(q)),
                q.text           = $text,
                q.weight         = $weight,
                q.iso_control_id = $iso_control_id,
                q.created_at     = $now,
                q.updated_at     = $now
            ON MATCH SET
                q.updated_at     = $now
            RETURN q.id AS qid
            """,
            key=key, text=text, weight=weight,
            iso_control_id=iso_control_id, now=_now_iso(),
        )
        record = await result.single()
        return record["qid"] if record else key
    return key


async def _link_question_to_control(
    session, question_id: str, control_id_val: str, framework_name: str, dry_run: bool
):
    """Create MERGE (control)-[:HAS_QUESTION]->(question)."""
    if not dry_run:
        await session.run(
            """
            MATCH (q:question {id: $qid})
            MATCH (c:control {control_id: $cid, framework: $fw})
            MERGE (c)-[:HAS_QUESTION]->(q)
            """,
            qid=question_id, cid=control_id_val, fw=framework_name,
        )


# ---------------------------------------------------------------------------
# Main import logic
# ---------------------------------------------------------------------------

async def run_import(xlsx_path: str, neo4j_uri: str, neo4j_user: str, neo4j_password: str, dry_run: bool):
    df = _load_excel(xlsx_path)

    stats = {
        "rows": 0,
        "questions": 0,
        "controls": 0,
        "links": 0,
    }

    driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))

    try:
        async with driver.session() as session:
            # Ensure ISO 27001 framework exists
            await _merge_framework(session, ISO_FRAMEWORK_NAME, dry_run)

            # Ensure all other framework nodes exist
            for fw_name, _, _ in FRAMEWORK_COLUMNS:
                await _merge_framework(session, fw_name, dry_run)

            # Track which (framework, control_id) pairs we've already created
            # to avoid redundant merge calls (they're idempotent but saves RTTs in bulk)
            seen_controls: set[tuple[str, str]] = set()
            seen_questions: set[str] = set()  # dedup_key

            # Iterate rows; carry forward ISO control ID and name (col A, B)
            current_iso_control_id = ""
            current_iso_control_name = ""

            for _, row in df.iterrows():
                col_a = str(row.get("Control ID", "")).strip()
                col_b = str(row.get("Control", "")).strip()
                col_c = str(row.get("Question", "")).strip()

                if col_a:
                    current_iso_control_id = col_a
                if col_b:
                    current_iso_control_name = col_b

                if not col_c or not current_iso_control_id:
                    continue  # skip header carry-forward rows with no question

                stats["rows"] += 1
                question_text = col_c

                # Ensure ISO control exists
                iso_key = (ISO_FRAMEWORK_NAME, current_iso_control_id)
                if iso_key not in seen_controls:
                    await _merge_control(
                        session, ISO_FRAMEWORK_NAME, current_iso_control_id,
                        current_iso_control_name, "", dry_run,
                    )
                    seen_controls.add(iso_key)
                    stats["controls"] += 1

                # Create / merge canonical question node
                dkey = f"{current_iso_control_id}||{_normalize_text(question_text)}"
                if dkey not in seen_questions:
                    q_id = await _merge_question(
                        session, question_text, 1.0, current_iso_control_id, dry_run
                    )
                    seen_questions.add(dkey)
                    stats["questions"] += 1
                else:
                    # Fetch existing id for linking
                    if not dry_run:
                        r = await session.run(
                            "MATCH (q:question {dedup_key: $key}) RETURN q.id AS qid",
                            key=dkey,
                        )
                        rec = await r.single()
                        q_id = rec["qid"] if rec else dkey
                    else:
                        q_id = dkey

                # Link question -> ISO control
                await _link_question_to_control(
                    session, q_id, current_iso_control_id, ISO_FRAMEWORK_NAME, dry_run
                )
                stats["links"] += 1

                # Process each framework column pair
                for fw_name, id_col, desc_col in FRAMEWORK_COLUMNS:
                    raw_ids = str(row.get(id_col, "")).strip()
                    raw_desc = str(row.get(desc_col, "")).strip()
                    clause_ids = _parse_ids(raw_ids)

                    for clause_id in clause_ids:
                        # Derive per-clause description: look for "clause_id: description" pattern
                        clause_desc = ""
                        if raw_desc:
                            # Pattern: "CID: some text; CID2: text2"
                            match = re.search(
                                rf"(?:^|;)\s*{re.escape(clause_id)}\s*:\s*([^;]+)",
                                raw_desc,
                            )
                            clause_desc = match.group(1).strip() if match else raw_desc

                        fw_key = (fw_name, clause_id)
                        if fw_key not in seen_controls:
                            await _merge_control(
                                session, fw_name, clause_id, clause_desc, "", dry_run
                            )
                            seen_controls.add(fw_key)
                            stats["controls"] += 1

                        # Link question -> this framework clause control
                        await _link_question_to_control(
                            session, q_id, clause_id, fw_name, dry_run
                        )
                        stats["links"] += 1

    finally:
        await driver.close()

    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Import Question Framework Mapping Excel into Neo4j")
    parser.add_argument(
        "--xlsx",
        default=str(Path(__file__).parent.parent.parent / "Question_Framework_Mapping.xlsx"),
        help="Path to the Excel file (default: project root)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report without writing to Neo4j",
    )
    args = parser.parse_args()

    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "")

    if not neo4j_password and not args.dry_run:
        print("ERROR: NEO4J_PASSWORD not set. Use --dry-run to test without a DB.")
        sys.exit(1)

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Importing: {args.xlsx}")
    print(f"Neo4j: {neo4j_uri} (user={neo4j_user})")

    stats = asyncio.run(run_import(
        xlsx_path=args.xlsx,
        neo4j_uri=neo4j_uri,
        neo4j_user=neo4j_user,
        neo4j_password=neo4j_password,
        dry_run=args.dry_run,
    ))

    print("\n=== Import Summary ===")
    print(f"  Rows processed : {stats['rows']}")
    print(f"  Questions       : {stats['questions']}")
    print(f"  Controls        : {stats['controls']}")
    print(f"  HAS_QUESTION    : {stats['links']}")
    print("Done.")


if __name__ == "__main__":
    main()
