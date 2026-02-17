from fastapi import HTTPException
from neo4j import AsyncSession


async def compute_threat_info(threat_id: str, asset_value: str, db: AsyncSession):
    """
    Compute risk information for a threat with multiple vulnerabilities.
    
    Now supports:
    - Many-to-many relationship between threat and vulnerabilities
    - Ease of exploitation is user-defined per vulnerability (not derived from control rating)
    - Risk is calculated per vulnerability, then aggregated to worst case
    """
    
    # Query threat with multiple vulnerabilities and their controls
    query = """
        MATCH (t:threat {id: $threat_id})
        OPTIONAL MATCH (v:vulnerability)-[:CAUSES_THREAT]->(t)
        OPTIONAL MATCH (c:control)-[:MITIGATES]->(v)
        RETURN t.threat_name AS threat_name, 
               t.likelihood AS likelihood,
               t.vulnerabilities AS vulnerability_ids,
               collect(DISTINCT {
                   vuln_id: v.id,
                   vuln_name: v.vulnerability_name,
                   ease_of_exploitation: v.ease_of_exploitation
               }) AS vulnerabilities,
               collect(DISTINCT c.id) AS control_ids
    """

    result = await db.run(query, threat_id=threat_id)
    record = await result.single()

    if not record or not record.get("threat_name"):
        raise HTTPException(status_code=404, detail="Threat not found")

    threat_name = record["threat_name"]
    likelihood = record["likelihood"]
    vulnerabilities_data = record["vulnerabilities"]
    control_ids = [c for c in record["control_ids"] if c is not None]
    
    # Filter out None/empty vulnerability entries
    vulnerabilities_data = [v for v in vulnerabilities_data if v.get("vuln_id")]
    
    # If no vulnerabilities found, try getting from the threat node's vulnerabilities property
    if not vulnerabilities_data:
        vulnerability_ids = record.get("vulnerability_ids") or []
        if isinstance(vulnerability_ids, str):
            vulnerability_ids = [vulnerability_ids]
        
        # Fetch vulnerability details
        if vulnerability_ids:
            vuln_query = """
                MATCH (v:vulnerability)
                WHERE v.id IN $vuln_ids
                OPTIONAL MATCH (c:control)-[:MITIGATES]->(v)
                RETURN v.id AS vuln_id, 
                       v.vulnerability_name AS vuln_name,
                       v.ease_of_exploitation AS ease_of_exploitation,
                       collect(DISTINCT c.id) AS ctrl_ids
            """
            vuln_result = await db.run(vuln_query, vuln_ids=vulnerability_ids)
            vuln_records = await vuln_result.data()
            
            for vr in vuln_records:
                vulnerabilities_data.append({
                    "vuln_id": vr["vuln_id"],
                    "vuln_name": vr["vuln_name"],
                    "ease_of_exploitation": vr["ease_of_exploitation"]
                })
                control_ids.extend([c for c in vr["ctrl_ids"] if c is not None])
    
    # Remove duplicate control IDs
    control_ids = list(set(control_ids))

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
    
    RISK_SEVERITY_ORDER = ["Low", "Medium", "High", "Very High"]
    
    # Calculate risk for each vulnerability and get worst case
    worst_risk = "Low"
    worst_ease = "Low"
    vulnerability_ids = []
    
    for vuln in vulnerabilities_data:
        vuln_id = vuln.get("vuln_id")
        if vuln_id:
            vulnerability_ids.append(vuln_id)
        
        # Get ease_of_exploitation from vulnerability (user-defined)
        ease_of_exploitation = vuln.get("ease_of_exploitation") or "Medium"
        
        try:
            risk = RISK_MATRIX[likelihood][asset_value][ease_of_exploitation]
        except KeyError:
            risk = "Unknown"
        
        # Track worst risk
        if risk in RISK_SEVERITY_ORDER:
            if RISK_SEVERITY_ORDER.index(risk) > RISK_SEVERITY_ORDER.index(worst_risk):
                worst_risk = risk
                worst_ease = ease_of_exploitation
    
    # If no vulnerabilities, default ease
    if not vulnerabilities_data:
        worst_ease = "Medium"
        try:
            worst_risk = RISK_MATRIX[likelihood][asset_value][worst_ease]
        except KeyError:
            worst_risk = "Unknown"

    return {
        "threat_name": threat_name,
        "likelihood": likelihood,
        "vulnerabilities": vulnerability_ids,
        "control_ids": control_ids,
        "ease_of_exploitation": worst_ease,
        "risk": worst_risk
    }