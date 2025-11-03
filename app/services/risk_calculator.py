from fastapi import HTTPException
from neo4j import AsyncSession


async def compute_threat_info(threat_id: str, asset_value: str, db: AsyncSession):
    query = """
        MATCH (t:threat {id: $threat_id})
        OPTIONAL MATCH (t)-[:CAUSES_THREAT]->(v:vulnerability)
        OPTIONAL MATCH (t)<-[:MITIGATES]-(c:control)
        RETURN t.threat_name AS threat_name, 
               t.likelihood AS likelihood,
               t.vulnerability AS vulnerability,
               c.control_id AS control_id,
               c.rating AS control_rating
    """

    result = await db.run(query, threat_id=threat_id)
    record = await result.single()
    id = record["vulnerability"]
    vulnerability_query = """
        MATCH (v:vulnerability {id: $id})
        RETURN v.vulnerability_name AS vulnerability_name
    """
    vuln_result = await db.run(vulnerability_query, id=id)
    vuln_record = await vuln_result.single()

    if not record or not record.get("threat_name"):
        raise HTTPException(status_code=404, detail="Threat not found")

    threat_name = record["threat_name"]
    likelihood = record["likelihood"]
    vulnerabilities = vuln_record["vulnerability_name"]
    control_name = record["control_id"]
    control_rating = record.get("control_rating")
    if control_rating is None:
        control_rating = "Low"  # Default if no control rating found
    ease_map = {
        "High": "Low",
        "Medium": "Medium",
        "Low": "High"
    }

    ease_of_exploitation = ease_map.get(str(control_rating).strip(), "Unknown")

    RISK_MATRIX = {
        "Low": {
            "Low":   {"Low": "Low", "Medium": "Low", "High": "Medium"},
            "Medium": {"Low": "Low", "Medium": "Medium", "High": "Medium"},
            "High": {"Low": "Medium", "Medium": "Medium", "High": "High"},
        },
        "Medium": {
            "Low":   {"Low": "Low", "Medium": "Medium", "High": "Medium"},
            "Medium": {"Low": "Medium", "Medium": "Medium", "High": "Medium"},
            "High": {"Low": "Medium", "Medium": "High", "High": "Very High"},
        },
        "High": {
            "Low":   {"Low": "Medium", "Medium": "Medium", "High": "Medium"},
            "Medium": {"Low": "Medium", "Medium": "Medium", "High": "High"},
            "High": {"Low": "High", "Medium": "Very High", "High": "Very High"},
        }
    }

    try:
        risk = RISK_MATRIX[likelihood][asset_value][ease_of_exploitation]
    except KeyError:
        risk = "Unknown"

    return {
        "threat_name": threat_name,
        "likelihood": likelihood,
        "vulnerabilities": vulnerabilities,
        "control_id": control_name,
        "ease_of_exploitation": ease_of_exploitation,
        "risk": risk
    }