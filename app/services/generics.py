from neo4j import AsyncSession
from services.schema_loader import load_schema
import uuid
from neo4j import AsyncDriver
import json



async def get_link_options_service(
    driver: AsyncDriver,
    document_type: str,
    search_fields: list[tuple[str, str]],
    filters: str = None,
    limit: int = 10,
    offset: int = 0,
):
    try:
        filter_conditions = []
        params = {
            "limit": limit,
            "offset": offset,
        }

        # Add search field filters (OR condition)
        search_clauses = []
        for idx, (field, term) in enumerate(search_fields):
            if term:
                param_key = f"search_term_{idx}"
                search_clauses.append(f"toLower(n.{field}) CONTAINS toLower(${param_key})")
                params[param_key] = term
        if search_clauses:
            filter_conditions.append(f"({' OR '.join(search_clauses)})")

        # Add filters (AND condition)
        if filters:
            filters_dict = json.loads(filters)
            for idx, (key, val) in enumerate(filters_dict.items()):
                filter_key = f"filter_{idx}"
                filter_conditions.append(f"n.{key} = ${filter_key}")
                params[filter_key] = val

        where_clause = " AND ".join(filter_conditions)
        where_query = f"WHERE {where_clause}" if where_clause else ""

        # Use the first field for label in dropdown
        label_field = search_fields[0][0] if search_fields else "id"

        cypher = f"""
        MATCH (n:{document_type})
        {where_query}
        RETURN n.id AS value, n.{label_field} AS label
        SKIP $offset
        LIMIT $limit
        """

        result = await driver.run(cypher, params)
        return await result.data()

    except Exception as e:
        raise Exception(f"Error in get_link_options_service: {str(e)}")