from fastapi import APIRouter
from services.neo4j_client import get_neo4j_driver

router = APIRouter(tags=["System"])

@router.get("/health")
def health_check():
    return {"status": "ok"}

@router.get("/test-neo4j")
async def test_neo4j():
    driver = get_neo4j_driver()
    async with driver.session() as session:
        result = await session.run("RETURN 'Neo4j is connected!' AS message")
        record = await result.single()
        msg = record["message"]
        return {"neo4j_status": msg}