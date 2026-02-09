from fastapi import HTTPException
from neo4j import AsyncSession


async def compute_threat_info(threat_id: str, asset_value: str, db: AsyncSession, asset_controls: list = None):
    """
    Compute threat info considering many-to-many relationship between controls and vulnerabilities.
    
    Risk is calculated using:
    - Likelihood: from threat
    - Ease of exploitation: from vulnerability
    - Asset value: from asset
    
    Args:
        threat_id: The threat ID to compute risk for
        asset_value: The asset value (Low/Medium/High)
        db: Database session
        asset_controls: List of control IDs attached to the asset (used to collect relevant controls)
    """
    # Get threat info and all associated vulnerabilities
    query = """
        MATCH (t:threat {id: $threat_id})
        OPTIONAL MATCH (t)-[:CAUSES_THREAT]->(v:vulnerability)
        RETURN t.threat_name AS threat_name, 
               t.likelihood AS likelihood,
               t.vulnerability AS vulnerability_id,
               collect(DISTINCT {id: v.id, ease: v.ease_of_exploitation}) AS vulnerabilities_data
    """

    result = await db.run(query, threat_id=threat_id)
    record = await result.single()

    if not record or not record.get("threat_name"):
        raise HTTPException(status_code=404, detail="Threat not found")

    threat_name = record["threat_name"]
    likelihood = record["likelihood"]
    vulnerability_id = record["vulnerability_id"]
    vulnerabilities_data = record.get("vulnerabilities_data", [])
    
    # Filter out None values and extract vulnerability IDs and ease of exploitation
    vulnerability_ids = []
    ease_of_exploitation_values = []
    
    for v in vulnerabilities_data:
        if v.get("id"):
            vulnerability_ids.append(v["id"])
            if v.get("ease"):
                ease_of_exploitation_values.append(v["ease"])
    
    # If no vulnerabilities from relationship, try to get from direct vulnerability field
    if not vulnerability_ids and vulnerability_id:
        vulnerability_ids = [vulnerability_id]
        # Fetch ease_of_exploitation from the vulnerability node
        vuln_query = """
            MATCH (v:vulnerability {id: $vuln_id})
            RETURN v.ease_of_exploitation AS ease
        """
        vuln_result = await db.run(vuln_query, vuln_id=vulnerability_id)
        vuln_record = await vuln_result.single()
        if vuln_record and vuln_record.get("ease"):
            ease_of_exploitation_values.append(vuln_record["ease"])
    
    # Determine ease of exploitation from vulnerabilities
    # If multiple vulnerabilities, use the highest (worst) ease of exploitation
    if ease_of_exploitation_values:
        ease_priority = {"High": 3, "Medium": 2, "Low": 1}
        ease_of_exploitation = max(ease_of_exploitation_values, key=lambda e: ease_priority.get(str(e).strip(), 0))
    else:
        # Default to High if no ease_of_exploitation found
        ease_of_exploitation = "High"
    
    # Collect all controls that mitigate vulnerabilities associated with this threat
    # Handles many-to-many: multiple controls can mitigate one vulnerability
    all_control_ids = []
    
    for vuln_id in vulnerability_ids:
        # Get all controls that mitigate this vulnerability (many-to-many relationship)
        control_query = """
            MATCH (c:control)-[:MITIGATES]->(v:vulnerability {id: $vuln_id})
            RETURN c.id AS control_node_id, c.control_id AS control_id
        """
        control_result = await db.run(control_query, vuln_id=vuln_id)
        control_records = await control_result.data()
        
        for ctrl_record in control_records:
            ctrl_node_id = ctrl_record.get("control_node_id")
            ctrl_id = ctrl_record.get("control_id")
            
            # If asset_controls is provided, only include controls attached to the asset
            if asset_controls:
                if ctrl_node_id in asset_controls or str(ctrl_id) in [str(c) for c in asset_controls]:
                    if ctrl_id and ctrl_id not in all_control_ids:
                        all_control_ids.append(ctrl_id)
            else:
                if ctrl_id and ctrl_id not in all_control_ids:
                    all_control_ids.append(ctrl_id)

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

    # Return vulnerabilities as a list for many-to-many support
    vulnerabilities_result = vulnerability_ids if vulnerability_ids else (vulnerability_id if vulnerability_id else None)
    
    return {
        "threat_name": threat_name,
        "likelihood": likelihood,
        "vulnerabilities": vulnerabilities_result,
        "control_ids": all_control_ids,  # List of control IDs (many-to-many)
        "control_id": all_control_ids[0] if all_control_ids else None,  # Backward compatibility
        "ease_of_exploitation": ease_of_exploitation,
        "risk": risk
    }