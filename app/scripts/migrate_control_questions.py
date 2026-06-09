"""
migrate_control_questions.py
-----------------------------
One-off migration that converts legacy data into the new question-centric model:

1. Convert each legacy `control_question` node's questions_text[] / weights[]
   into individual canonical `question` nodes linked to the control via HAS_QUESTION.
   Deduplicates against existing question nodes by (iso_control_id, normalized text).

2. Convert each `control_assessment` node's serialized `control_assessment` JSON
   answer list into `question_response` nodes per (question_id, organization_id).

3. Recompute derived `control_compliance` for every existing (control, org) pair
   using the new weighted answer logic.

Run from the project root:
    python app/scripts/migrate_control_questions.py [--dry-run]

Environment variables (or .env file):
    NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from neo4j import AsyncGraphDatabase

# ---------------------------------------------------------------------------
# Helpers (shared with importer)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _compute_compliance(answered: int, total: int, weighted_yes: float, weighted_total: float) -> str:
    """
    Derive compliance status from question responses.
    - All answered yes (weighted)  => Compliant
    - Some answered yes            => Partially Compliant
    - None answered yes            => Non Compliant
    """
    if total == 0:
        return "Non Compliant"
    if weighted_total <= 0:
        # fallback to simple count
        if answered == total:
            return "Compliant"
        if answered > 0:
            return "Partially Compliant"
        return "Non Compliant"
    ratio = weighted_yes / weighted_total
    if ratio >= 1.0:
        return "Compliant"
    if ratio > 0.0:
        return "Partially Compliant"
    return "Non Compliant"


# ---------------------------------------------------------------------------
# Migration phases
# ---------------------------------------------------------------------------

async def phase1_migrate_control_questions(session, dry_run: bool) -> dict:
    """
    Convert legacy control_question nodes into canonical question nodes.
    Returns stats dict.
    """
    stats = {"control_question_nodes": 0, "questions_created": 0, "links_created": 0, "skipped": 0}

    result = await session.run(
        """
        MATCH (cq:control_question)
        OPTIONAL MATCH (c:control)-[:HAS_QUESTION]->(cq)
        RETURN cq, c.control_id AS control_id_value, c.id AS control_node_id, c.framework AS fw_name
        """
    )
    records = await result.data()

    for rec in records:
        cq = dict(rec["cq"])
        control_id_val = rec.get("control_id_value") or cq.get("control", "")
        control_node_id = rec.get("control_node_id")
        fw_name = rec.get("fw_name") or "ISO 27001"

        questions_text = cq.get("questions_text") or []
        weights = cq.get("weights") or []

        if not questions_text:
            stats["skipped"] += 1
            continue

        stats["control_question_nodes"] += 1

        for i, text in enumerate(questions_text):
            weight = float(weights[i]) if i < len(weights) else 1.0
            dkey = f"{control_id_val}||{_normalize_text(text)}"

            # Check if canonical question already exists (from importer run)
            check_r = await session.run(
                "MATCH (q:question {dedup_key: $key}) RETURN q.id AS qid",
                key=dkey,
            )
            check_rec = await check_r.single()

            if check_rec:
                q_id = check_rec["qid"]
            else:
                # Create new canonical question node
                stats["questions_created"] += 1
                if not dry_run:
                    create_r = await session.run(
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
                        key=dkey, text=text, weight=weight,
                        iso_control_id=control_id_val, now=_now_iso(),
                    )
                    cr = await create_r.single()
                    q_id = cr["qid"] if cr else dkey
                else:
                    q_id = dkey

            # Link control -> question
            if control_node_id and not dry_run:
                await session.run(
                    """
                    MATCH (q:question {id: $qid})
                    MATCH (c:control {id: $cid})
                    MERGE (c)-[:HAS_QUESTION]->(q)
                    """,
                    qid=q_id, cid=control_node_id,
                )
                stats["links_created"] += 1

    return stats


async def phase2_migrate_assessment_answers(session, dry_run: bool) -> dict:
    """
    Convert control_assessment.control_assessment JSON answer arrays into
    question_response nodes.
    """
    stats = {"assessments": 0, "responses_created": 0, "skipped": 0}

    result = await session.run(
        """
        MATCH (a:control_assessment)
        WHERE a.control_assessment IS NOT NULL AND a.control_assessment <> ''
        RETURN a.control_id AS control_id, a.organization_id AS org_id,
               a.control_assessment AS answers_json
        """
    )
    records = await result.data()

    for rec in records:
        control_id_val = rec["control_id"]
        org_id = rec["org_id"]
        answers_raw = rec["answers_json"]

        try:
            answers = json.loads(answers_raw) if isinstance(answers_raw, str) else answers_raw
        except Exception:
            stats["skipped"] += 1
            continue

        if not isinstance(answers, list):
            stats["skipped"] += 1
            continue

        stats["assessments"] += 1

        for item in answers:
            if not isinstance(item, dict):
                continue
            q_text = str(item.get("question", "")).strip()
            answer_val = bool(item.get("answer", False))
            if not q_text:
                continue

            dkey = f"{control_id_val}||{_normalize_text(q_text)}"

            # Find the canonical question node for this answer
            q_result = await session.run(
                "MATCH (q:question {dedup_key: $key}) RETURN q.id AS qid",
                key=dkey,
            )
            q_rec = await q_result.single()
            if not q_rec:
                stats["skipped"] += 1
                continue

            q_id = q_rec["qid"]

            # Upsert question_response node
            if not dry_run:
                await session.run(
                    """
                    MERGE (r:question_response {question_id: $qid, organization_id: $org_id})
                    ON CREATE SET
                        r.id         = 'qr-' + toString(id(r)),
                        r.answer     = $answer,
                        r.evidence   = $evidence,
                        r.remarks    = $remarks,
                        r.created_at = $now,
                        r.updated_at = $now
                    ON MATCH SET
                        r.answer     = $answer,
                        r.updated_at = $now
                    WITH r
                    MATCH (q:question {id: $qid})
                    MERGE (q)-[:HAS_RESPONSE]->(r)
                    """,
                    qid=q_id,
                    org_id=org_id,
                    answer=answer_val,
                    evidence=str(item.get("evidence", "")),
                    remarks=str(item.get("remarks", "")),
                    now=_now_iso(),
                )
            stats["responses_created"] += 1

    return stats


async def phase3_recompute_compliance(session, dry_run: bool) -> dict:
    """
    Recompute control_compliance for every (control, org) pair from
    question_response data and upsert into control_assessment.
    """
    stats = {"assessments_updated": 0}

    # Fetch all (control, org) combos that have assessments
    result = await session.run(
        """
        MATCH (a:control_assessment)
        RETURN DISTINCT a.control_id AS control_id, a.organization_id AS org_id
        """
    )
    records = await result.data()

    for rec in records:
        control_id_val = rec["control_id"]
        org_id = rec["org_id"]

        # Get all questions linked to this control
        q_result = await session.run(
            """
            MATCH (c:control {control_id: $cid})-[:HAS_QUESTION]->(q:question)
            OPTIONAL MATCH (q)-[:HAS_RESPONSE]->(r:question_response {organization_id: $org_id})
            RETURN q.weight AS weight, r.answer AS answer
            """,
            cid=control_id_val, org_id=org_id,
        )
        q_records = await q_result.data()

        if not q_records:
            continue

        total = len(q_records)
        answered = sum(1 for r in q_records if r.get("answer") is True)
        weighted_total = sum(float(r.get("weight") or 1.0) for r in q_records)
        weighted_yes = sum(
            float(r.get("weight") or 1.0) for r in q_records if r.get("answer") is True
        )

        new_compliance = _compute_compliance(answered, total, weighted_yes, weighted_total)

        if not dry_run:
            await session.run(
                """
                MATCH (a:control_assessment {control_id: $cid, organization_id: $org_id})
                SET a.control_compliance = $compliance, a.updated_at = $now
                """,
                cid=control_id_val, org_id=org_id,
                compliance=new_compliance, now=_now_iso(),
            )
        stats["assessments_updated"] += 1

    return stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_migration(neo4j_uri: str, neo4j_user: str, neo4j_password: str, dry_run: bool):
    driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
    try:
        async with driver.session() as session:
            print("Phase 1: Migrating legacy control_question nodes...")
            s1 = await phase1_migrate_control_questions(session, dry_run)
            print(f"  control_question nodes: {s1['control_question_nodes']}")
            print(f"  questions created     : {s1['questions_created']}")
            print(f"  HAS_QUESTION links    : {s1['links_created']}")
            print(f"  skipped               : {s1['skipped']}")

            print("\nPhase 2: Migrating control_assessment answer arrays...")
            s2 = await phase2_migrate_assessment_answers(session, dry_run)
            print(f"  assessments processed : {s2['assessments']}")
            print(f"  question_responses    : {s2['responses_created']}")
            print(f"  skipped               : {s2['skipped']}")

            print("\nPhase 3: Recomputing control compliance from responses...")
            s3 = await phase3_recompute_compliance(session, dry_run)
            print(f"  assessments updated   : {s3['assessments_updated']}")
    finally:
        await driver.close()


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Migrate legacy control questions to canonical question nodes")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = parser.parse_args()

    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "")

    if not neo4j_password and not args.dry_run:
        print("ERROR: NEO4J_PASSWORD not set.")
        sys.exit(1)

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Migration starting...")
    asyncio.run(run_migration(neo4j_uri, neo4j_user, neo4j_password, args.dry_run))
    print("\nMigration complete.")


if __name__ == "__main__":
    main()
