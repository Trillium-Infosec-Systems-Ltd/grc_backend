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
    filter_for_doctype: str = None,
):
    try:
        filter_conditions = []
        params = {
            "limit": limit,
            "offset": offset,
        }

        # If filter_for_doctype is specified, auto-detect the field that links to document_type
        # and limit results to values that exist in that doctype
        if filter_for_doctype:
            try:
                parent_schema = load_schema(filter_for_doctype)
                # Find the field in parent schema that links to current document_type
                field_name = None
                for f in parent_schema.get("fields", []):
                    if f.get("link_to") == document_type and f.get("fieldtype") in ["Link", "MultiLink"]:
                        field_name = f.get("fieldname")
                        break
                
                if field_name:
                    # Query to get distinct IDs used in that field
                    filter_query = f"""
                    MATCH (p:{filter_for_doctype})
                    WHERE p.{field_name} IS NOT NULL
                    UNWIND 
                        CASE WHEN p.{field_name} IS NULL THEN [] 
                             WHEN p.{field_name} = '' THEN []
                             ELSE CASE WHEN size(p.{field_name}) > 0 THEN p.{field_name} ELSE [p.{field_name}] END 
                        END AS used_id
                    RETURN DISTINCT used_id
                    """
                    result = await driver.run(filter_query)
                    used_ids = [record["used_id"] for record in await result.data()]
                    print(f"[filter_for_doctype] Found {len(used_ids)} distinct {document_type} IDs in {filter_for_doctype}.{field_name}")
                    if used_ids:
                        filter_conditions.append("n.id IN $used_ids")
                        params["used_ids"] = used_ids
                    else:
                        # No values exist, return empty list
                        return []
            except Exception as e:
                print(f"[filter_for_doctype] Error: {e}")
                # Continue without filter on error

        # Add search field filters (OR condition)
        search_clauses = []
        for idx, (field, term) in enumerate(search_fields):
            if term:
                param_key = f"search_term_{idx}"
                # ✅ cast property to string before toLower()
                search_clauses.append(f"toLower(toString(n.{field})) CONTAINS toLower(${param_key})")
                params[param_key] = str(term)  # also cast param to string
        if search_clauses:
            filter_conditions.append(f"({' OR '.join(search_clauses)})")

        # Add filters (AND condition)
        if filters:
            filters_dict = json.loads(filters)
            for idx, (key, val) in enumerate(filters_dict.items()):
                filter_key = f"filter_{idx}"
                # Handle list values with IN clause
                if isinstance(val, list):
                    filter_conditions.append(f"n.{key} IN ${filter_key}")
                    params[filter_key] = val
                else:
                    # ✅ cast filter fields to string too
                    filter_conditions.append(f"toString(n.{key}) = ${filter_key}")
                    params[filter_key] = str(val)

        where_clause = " AND ".join(filter_conditions)
        where_query = f"WHERE {where_clause}" if where_clause else ""

        # Use the first field for label in dropdown
        label_field = search_fields[0][0] if search_fields else "id"
    
        # Add sorting for controls to handle version-style IDs (5.1, 5.2, ..., 5.10)
        if document_type == "control":
            order_clause = """ORDER BY 
                toInteger(split(toString(n.control_id), '.')[0]) ASC,
                CASE WHEN size(split(toString(n.control_id), '.')) > 1 
                     THEN toInteger(split(toString(n.control_id), '.')[1]) 
                     ELSE 0 END ASC"""
        elif document_type == "control_question":
            order_clause = """ORDER BY 
                toInteger(split(toString(n.control), '.')[0]) ASC,
                CASE WHEN size(split(toString(n.control), '.')) > 1 
                     THEN toInteger(split(toString(n.control), '.')[1]) 
                     ELSE 0 END ASC"""
        else:
            order_clause = ""

        cypher = f"""
        MATCH (n:{document_type})
        {where_query}
        RETURN n.id AS value, n.{label_field} AS label
        {order_clause}
        SKIP $offset
        LIMIT $limit
            """

        result = await driver.run(cypher, params)
        return await result.data()

    except Exception as e:
        raise Exception(f"Error in get_link_options_service: {str(e)}")
