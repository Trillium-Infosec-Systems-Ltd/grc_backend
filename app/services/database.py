from typing import AsyncGenerator
from neo4j._async.work.session import AsyncSession
from .neo4j_client import get_neo4j_driver

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    driver = get_neo4j_driver()
    async with driver.session() as session:
        yield session
