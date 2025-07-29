from fastapi import APIRouter, Depends, HTTPException
from neo4j import AsyncDriver
from uuid import uuid4
from neo4j import AsyncSession
from services.database import get_db
from services.dependencies import get_current_user
from fastapi import Body
from fastapi import Query
from schemas.user_schema import UserCreate, UserLogin, UserUpdate, RefreshTokenRequest,OrgSwitchRequest
from services.auth_service import hash_password, verify_password, create_access_token, create_refresh_token, \
    decode_token
from fastapi import Body

# router = APIRouter(prefix="/auth", tags=["Auth"])
router = APIRouter()


@router.post("/users")
async def register(user: UserCreate, session: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)):
    creator_role = current_user.get("role")
    target_role = user.role

    if creator_role == "user":
        raise HTTPException(status_code=403, detail="Users cannot create other users")
    if creator_role == "partner" and target_role != "user":
        raise HTTPException(status_code=403, detail="Partners can only create users")
    if creator_role == "super_admin" and target_role not in ["partner", "user"]:
        raise HTTPException(status_code=403, detail="Super Admin can only create users or partners")

    result = await session.run("MATCH (u:users {email: $email}) RETURN u", email=user.email)
    if await result.single():
        raise HTTPException(status_code=400, detail="User already exists")

    if user.username:
        result = await session.run("MATCH (u:users {username: $username}) RETURN u", username=user.username)
        if await result.single():
            raise HTTPException(status_code=400, detail="Username already exists")

    counter_query = """
    MERGE (c:Counter {doctype: 'users'})
    ON CREATE SET c.current = 1
    ON MATCH SET c.current = c.current + 1
    RETURN c.current AS new_id
    """
    counter_result = await session.run(counter_query)
    user_id = f"users-{(await counter_result.single())["new_id"]}"

    await session.run("""
    CREATE (u:users {
        id: $id,
        name: $name,
        username: $username,
        email: $email,
        password: $password,
        role: $role,
        date_of_birth: $date_of_birth,
        present_address: $present_address,
        permanent_address: $permanent_address,
        city: $city,
        postal_code: $postal_code,
        country: $country
    }) RETURN u
    """, id=user_id, name=user.name, username=user.username or f"user{user_id}", email=user.email,
        password=hash_password(user.password), role=user.role, date_of_birth=user.date_of_birth,
        present_address=user.present_address, permanent_address=user.permanent_address,
        city=user.city, postal_code=user.postal_code, country=user.country)

    for org_id in user.org_id:
        await session.run("""
        MATCH (u:users {id: $user_id})
        MATCH (o:organization {id: $org_id})
        MERGE (u)-[:ASSOCIATE_WITH]->(o)
        """, user_id=user_id, org_id=org_id)

    return {"msg": "User registered successfully", "id": user_id}
@router.get("/users")
async def get_users(
        session: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user),
        skip: int = Query(0, ge=0),
        limit: int = Query(10, ge=1, le=100)
):
    creator_role = current_user.get("role")
    creator_user_id = current_user.get("id")
    creator_org_ids = current_user.get("org_id") if isinstance(current_user.get("org_id"), list) else [current_user.get("org_id")]

    # Step 1: Super Admin - view all users
    if creator_role == "super_admin":
        total_query = """
        MATCH (u:users)
        WHERE u.id <> $creator_user_id
        RETURN count(u) AS total
        """
        result_total = await session.run(total_query, creator_user_id=creator_user_id)
        total = (await result_total.single())["total"]

        paginated_query = """
        MATCH (u:users)
        WHERE u.id <> $creator_user_id
        OPTIONAL MATCH (u)-[:ASSOCIATE_WITH]->(o:organization)
        WITH u, collect(o.id) AS org_ids
        RETURN u, org_ids
        SKIP $skip LIMIT $limit
        """
        result = await session.run(paginated_query, creator_user_id=creator_user_id, skip=skip, limit=limit)

        users = []
        async for record in result:
            u = record["u"]
            org_ids = record["org_ids"]
            users.append({
                "id": u["id"],
                "name": u.get("name"),
                "username": u.get("username"),
                "email": u.get("email"),
                "password": u.get("password"),
                "role": u.get("role"),
                "org_id": org_ids
            })

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": users
        }

    # Step 2: Partner - users within their orgs
    if creator_role == "partner":
        total_query = """
        MATCH (u:users)-[:ASSOCIATE_WITH]->(o:organization)
        WHERE o.id IN $org_ids AND u.id <> $creator_user_id
        RETURN count(DISTINCT u) AS total
        """
        result_total = await session.run(total_query, org_ids=creator_org_ids, creator_user_id=creator_user_id)
        total = (await result_total.single())["total"]

        paginated_query = """
        MATCH (u:users)-[:ASSOCIATE_WITH]->(o:organization)
        WHERE o.id IN $org_ids AND u.id <> $creator_user_id
        OPTIONAL MATCH (u)-[:ASSOCIATE_WITH]->(o2:organization)
        WITH u, collect(DISTINCT o2.id) AS org_ids
        RETURN u, org_ids
        SKIP $skip LIMIT $limit
        """
        result = await session.run(paginated_query, org_ids=creator_org_ids, creator_user_id=creator_user_id, skip=skip, limit=limit)

        users = []
        async for record in result:
            u = record["u"]
            org_ids = record["org_ids"]
            users.append({
                "id": u["id"],
                "name": u.get("name"),
                "username": u.get("username"),
                "email": u.get("email"),
                "password": u.get("password"),
                "role": u.get("role"),
                "org_id": org_ids
            })

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": users
        }

    # Step 3: Regular user - can only view themselves
    if creator_role == "user":
        query = """
        MATCH (u:users {id: $user_id}) 
        OPTIONAL MATCH (u)-[:ASSOCIATE_WITH]->(o:organization)
        WITH u, collect(o.id) AS org_ids
        RETURN u, org_ids
        """
        result = await session.run(query, user_id=creator_user_id)
        record = await result.single()
        if not record:
            raise HTTPException(status_code=404, detail="User not found")

        u = record["u"]
        org_ids = record["org_ids"]

        return {
            "total": 1,
            "skip": 0,
            "limit": 1,
            "items": [{
                "id": u["id"],
                "name": u.get("name"),
                "username": u.get("username"),
                "email": u.get("email"),
                "password": u.get("password"),
                "role": u.get("role"),
                "org_id": org_ids
            }]
        }

    raise HTTPException(status_code=403, detail="Unauthorized to view users")


@router.put("/users/{user_id}")
async def update_user(user_id: str, user: UserUpdate, session: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)):
    result = await session.run("MATCH (u:users {id: $user_id}) RETURN u", user_id=user_id)
    existing_user = await result.single()
    if not existing_user:
        raise HTTPException(status_code=404, detail="User not found")

    creator_role = current_user.get("role")
    target_user = existing_user["u"]
    if creator_role == "user":
        raise HTTPException(status_code=403, detail="Users cannot update other users")
    if creator_role == "partner" and target_user["role"] != "user":
        raise HTTPException(status_code=403, detail="Partners can only update users")
    if creator_role == "super_admin" and target_user["role"] not in ["partner", "user"]:
        raise HTTPException(status_code=403, detail="Super Admin can only update users or partners")

    update_fields = {}
    if user.name: update_fields["name"] = user.name
    if user.username:
        username_check_query = """
        MATCH (u:users)
        WHERE u.username = $username AND u.id <> $user_id
        RETURN u LIMIT 1
        """
        if await (await session.run(username_check_query, username=user.username, user_id=user_id)).single():
            raise HTTPException(status_code=400, detail="Username already exists")
        update_fields["username"] = user.username
    if user.email: update_fields["email"] = user.email
    if user.password: update_fields["password"] = hash_password(user.password)
    if user.role: update_fields["role"] = user.role
    if user.date_of_birth: update_fields["date_of_birth"] = user.date_of_birth
    if user.present_address: update_fields["present_address"] = user.present_address
    if user.permanent_address: update_fields["permanent_address"] = user.permanent_address
    if user.city: update_fields["city"] = user.city
    if user.postal_code: update_fields["postal_code"] = user.postal_code
    if user.country: update_fields["country"] = user.country

    await session.run("""
    MATCH (u:users {id: $user_id})
    SET u += $update_fields
    RETURN u
    """, user_id=user_id, update_fields=update_fields)

    if user.org_id:
        await session.run("MATCH (u:users {id: $user_id})-[r:ASSOCIATE_WITH]->() DELETE r", user_id=user_id)
        for org_id in user.org_id:
            await session.run("""
            MATCH (u:users {id: $user_id})
            MATCH (o:organization {id: $org_id})
            MERGE (u)-[:ASSOCIATE_WITH]->(o)
            """, user_id=user_id, org_id=org_id)

    return {"msg": "User updated successfully", "id": user_id}


@router.get("/users/{user_id}")
async def get_user_by_id(
    user_id: str,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    query = """
    MATCH (u:users {id: $user_id})
    OPTIONAL MATCH (u)-[:ASSOCIATE_WITH]->(o:organization)
    RETURN u, collect(o.id) AS org_ids
    """
    result = await session.run(query, user_id=user_id)
    record = await result.single()
    if not record:
        raise HTTPException(status_code=404, detail="User not found")

    u = dict(record["u"])  # ✅ Convert Node to dict
    org_ids = record["org_ids"]

    creator_role = current_user.get("role")
    if creator_role == "user" and user_id != current_user.get("id"):
        raise HTTPException(status_code=403, detail="You cannot view other users")

    if creator_role == "partner" and not set(org_ids).intersection(set(current_user.get("org_id", []))):
        raise HTTPException(status_code=403, detail="You can only view users within your organization")

    u["org_id"] = org_ids  # ✅ Now this works

    return {"user": u}


@router.post("/login")
async def login(user: UserLogin, session: AsyncSession = Depends(get_db)):
    result = await session.run("MATCH (u:users {email: $email}) RETURN u", email=user.email)
    record = await result.single()
    if not record:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    db_user = record["u"]
    if not verify_password(user.password, db_user["password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Get all organizations
    org_result = await session.run("""
        MATCH (u:users {id: $id})-[:ASSOCIATE_WITH]->(o:organization)
        RETURN collect({name: o.organization_name, id: o.id}) AS org_data
    """, id=db_user["id"])
    org_data = (await org_result.single())["org_data"]

    # Select first org as active
    active_org = org_data[0]["id"] if org_data else None

    all_orgs = [{"label": org["name"], "value": org["id"]} for org in org_data]


    token_data = {
        "id": db_user["id"],
        "email": db_user["email"],
        "role": db_user["role"],
        "name": db_user.get("name"),
        "username": db_user.get("username"),
        "org_id": active_org
    }

    return {
        "access_token": create_access_token(token_data),
        "refresh_token": create_refresh_token(token_data, remember_me=user.remember_me),
        "token_type": "bearer",
        **token_data,
        "all_org_ids": all_orgs
    }



@router.post("/refresh")
async def refresh_token(body: RefreshTokenRequest):
    payload = decode_token(body.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    return {
        "access_token": create_access_token({
            "sub": payload["sub"],
            "email": payload["email"],
            "role": payload["role"]
        }),
        "token_type": "bearer"
    }



@router.get("/switch-org")
async def switch_organization(
    org_id: str = Query(...),
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db)
):
    user_id = current_user["id"]

    # Verify the user is associated with the requested org
    validation_result = await session.run("""
        MATCH (u:users {id: $user_id})-[:ASSOCIATE_WITH]->(o:organization {id: $org_id})
        RETURN o
    """, user_id=user_id, org_id=org_id)
    if not await validation_result.single():
        raise HTTPException(status_code=403, detail="You are not associated with this organization.")

    # Fetch all orgs with names
    org_result = await session.run("""
        MATCH (u:users {id: $id})-[:ASSOCIATE_WITH]->(o:organization)
        RETURN collect({name: o.organization_name, id: o.id}) AS org_data
    """, id=user_id)
    org_data = (await org_result.single())["org_data"]
    all_orgs = [{"label": org["name"], "value": org["id"]} for org in org_data]


    token_data = {
        "id": current_user["id"],
        "email": current_user["email"],
        "role": current_user["role"],
        "name": current_user.get("name"),
        "username": current_user.get("username"),
        "org_id": org_id
    }

    return {
        "access_token": create_access_token(token_data),
        "refresh_token": create_refresh_token(token_data),
        "token_type": "bearer",
        **token_data,
        "all_org_ids": all_orgs
    }

@router.get("/me")
async def whoami(current_user: dict = Depends(get_current_user)):
    return current_user


