from neo4j import AsyncDriver
from fastapi import HTTPException

async def create_dynamic_relationship(driver: AsyncDriver, node1, node2, rel):
    async with driver.session() as session:
        # Check if both nodes exist
        node1_exists = await session.run(
            f"MATCH (n:{node1['label']} {{id: $id}}) RETURN n", {"id": node1['id']}
        )
        if not await node1_exists.single():
            raise HTTPException(status_code=404, detail="Node1 not found")

        node2_exists = await session.run(
            f"MATCH (n:{node2['label']} {{id: $id}}) RETURN n", {"id": node2['id']}
        )
        if not await node2_exists.single():
            raise HTTPException(status_code=404, detail="Node2 not found")

        # Merge the relationship
        prop_string = ", ".join([f"{k}: ${k}" for k in rel["properties"].keys()])
        query = f"""
        MATCH (a:{node1['label']} {{id: $id1}})
        MATCH (b:{node2['label']} {{id: $id2}})
        MERGE {"(a)-[r:" + rel['relation_type'] + " {" + prop_string + "}]->(b)" 
              if rel["direction"] == "OUTGOING" 
              else "(b)-[r:" + rel['relation_type'] + " {" + prop_string + "}]->(a)"}
        RETURN type(r) AS relationship_type
        """

        params = {"id1": node1["id"], "id2": node2["id"], **rel["properties"]}
        result = await session.run(query, params)
        record = await result.single()
        return record["relationship_type"]
