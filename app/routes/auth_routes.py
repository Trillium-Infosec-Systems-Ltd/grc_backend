from fastapi import APIRouter, Depends, HTTPException
from neo4j import AsyncDriver
from uuid import uuid4
from neo4j import AsyncSession
from services.database import get_db
from services.dependencies import get_current_user
from fastapi import Body
from fastapi import Query
from schemas.user_schema import UserCreate, UserLogin,UserUpdate,RefreshTokenRequest
from services.auth_service import hash_password, verify_password, create_access_token,create_refresh_token,decode_token

# router = APIRouter(prefix="/auth", tags=["Auth"])
router = APIRouter()

@router.post("/users")
async def register(user: UserCreate,  session: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)):
    # Step 1: Check if user already exists
    
    creator_role = current_user.get("role")
    target_role = user.role

    if creator_role == "user":
        raise HTTPException(status_code=403, detail="Users cannot create other users")
    
    if creator_role == "partner" and target_role != "user":
        raise HTTPException(
            status_code=403,
            detail="Partners can only create users"
        )

    if creator_role == "super_admin" and target_role not in ["partner", "user"]:
        raise HTTPException(
            status_code=403,
            detail="Super Admin can only create users or partners"
        )

    
    
    check_query = "MATCH (u:users {email: $email}) RETURN u"
    result = await session.run(check_query, email=user.email)
    if await result.single():
        raise HTTPException(status_code=400, detail="User already exists")

    # Step 2: Generate user ID using Counter
    counter_query = """
    MERGE (c:Counter {doctype: 'users'})
    ON CREATE SET c.current = 1
    ON MATCH SET c.current = c.current + 1
    RETURN c.current AS new_id
    """
    counter_result = await session.run(counter_query)
    counter_record = await counter_result.single()
    new_id = counter_record["new_id"]
    user_id = f"users-{new_id}"

    # Step 3: Create User node
    create_user_query = """
    CREATE (u:users {
        id: $id,
        name:$name,
        email: $email,
        password: $password,
        role: $role,
        org_id: $org_id
    }) RETURN u
    """
    await session.run(create_user_query, id=user_id,name = user.name,email=user.email,
                      password=hash_password(user.password),
                      role=user.role, org_id=user.org_id)

    # Step 4: Create relation from User → Organization
    relation_query = """
    MATCH (u:users {id: $user_id})
    MATCH (o:organization {id: $org_id})
    MERGE (u)-[:ASSOCIATE_WITH]->(o)
    """
    await session.run(relation_query, user_id=user_id, org_id=user.org_id)

    return {"msg": "users registered successfully", "id": user_id}

@router.put("/users/{user_id}")
async def update_user(user_id: str, user: UserUpdate, session: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)):
    # Step 1: Check if the user exists
    check_query = "MATCH (u:users {id: $user_id}) RETURN u"
    result = await session.run(check_query, user_id=user_id)
    existing_user = await result.single()
    
    if not existing_user:
        raise HTTPException(status_code=404, detail="User not found")

    # Step 2: Authorization check based on user role
    creator_role = current_user.get("role")
    target_user = existing_user["u"]
    
    if creator_role == "user":
        raise HTTPException(status_code=403, detail="Users cannot update other users")
    
    if creator_role == "partner" and target_user["role"] != "user":
        raise HTTPException(status_code=403, detail="Partners can only update users")
    
    if creator_role == "super_admin" and target_user["role"] not in ["partner", "user"]:
        raise HTTPException(status_code=403, detail="Super Admin can only update users or partners")
    
    # Step 3: Update the user's details (password, email, role, etc.)
    update_fields = {}
    if user.email:
        update_fields["email"] = user.email
    if user.password:
        update_fields["password"] = hash_password(user.password)
    if user.role:
        update_fields["role"] = user.role
    if user.org_id:
        update_fields["org_id"] = user.org_id

    if not update_fields:
        raise HTTPException(status_code=400, detail="No valid fields provided to update")

    update_query = """
    MATCH (u:users {id: $user_id})
    SET u.email = COALESCE($email, u.email),
        u.password = COALESCE($password, u.password),
        u.role = COALESCE($role, u.role),
        u.org_id = COALESCE($org_id, u.org_id)
    RETURN u
    """
    
    await session.run(update_query, user_id=user_id, **update_fields)

    # Step 4: If organization is updated, update relationship
    if "org_id" in update_fields:
        # First, remove the old relationship
        remove_relation_query = """
        MATCH (u:users {id: $user_id})-[r:ASSOCIATE_WITH]->(o:Organization)
        DELETE r
        """
        await session.run(remove_relation_query, user_id=user_id)

        # Then, create the new relationship
        add_relation_query = """
        MATCH (u:users {id: $user_id})
        MATCH (o:Organization {id: $org_id})
        MERGE (u)-[:ASSOCIATE_WITH]->(o)
        """
        await session.run(add_relation_query, user_id=user_id, org_id=user.org_id)

    return {"msg": "User updated successfully", "id": user_id}


@router.get("/users/{user_id}")
async def get_user_by_id(
    user_id: str,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    # Step 1: Check if user exists
    query = """
    MATCH (u:users {id: $user_id})
    OPTIONAL MATCH (u)-[r]->(n)
    RETURN u, collect(
        CASE 
            WHEN r IS NOT NULL AND n IS NOT NULL THEN {
                type: type(r),
                properties: properties(r),
                target_label: labels(n),
                target_node: n
            }
        END
    ) AS relationships
    """
    result = await session.run(query, user_id=user_id)
    record = await result.single()
    # 

    if not record:
        raise HTTPException(status_code=404, detail="User not found")

    target_user = record["u"]
    relationship = record["relationships"][0] if record["relationships"] else None

    relationship = dict(relationship["target_node"]) if relationship and relationship["target_label"][0] == "organization" else {}

    # Step 2: Authorization check
    creator_role = current_user.get("role")
    creator_user_id = current_user.get("id")
    creator_org_id = current_user.get("org_id")

    # Regular user can only view their own data
    if creator_role == "user" and user_id != creator_user_id:
        raise HTTPException(status_code=403, detail="You cannot view other users")

    # Partner can only view users in their organization
    if creator_role == "partner" and target_user.get("org_id") != creator_org_id:
        raise HTTPException(status_code=403, detail="You can only view users within your organization")

    return {"user": target_user,"relationships" : relationship}


@router.get("/users")
async def get_users(
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100)
):
    # import pdb;pdb.set_trace()
    creator_role = current_user.get("role")
    creator_user_id = current_user.get("id")
    print("======================",creator_user_id)
    creator_org_id = current_user.get("org_id")

    # Step 1: Super Admin - view all usersf
    if creator_role == "super_admin":
        total_query = """MATCH (u:users) 
        WHERE u.id <> $creator_user_id
        RETURN count(u) AS total"""
        result_total = await session.run(total_query,creator_user_id=creator_user_id)
        total = (await result_total.single())["total"]

        paginated_query = """
        MATCH (u:users)
        WHERE u.id <> $creator_user_id
        RETURN u
        SKIP $skip LIMIT $limit
        """
        result = await session.run(paginated_query,creator_user_id=creator_user_id, skip=skip, limit=limit)
        users = [record["u"] for record in await result.data()]

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": users
        }

    # Step 2: Partner - users within their org
    if creator_role == "partner":
        total_query = """
        MATCH (u:users)-[:ASSOCIATE_WITH]->(o:organization {id: $org_id})
        WHERE u.id <> $creator_user_id
        RETURN count(u) AS total
        """
        result_total = await session.run(total_query, org_id=creator_org_id,creator_user_id=creator_user_id)
        total = (await result_total.single())["total"]

        paginated_query = """
        MATCH (u:users)-[:ASSOCIATE_WITH]->(o:organization {id: $org_id})

        WHERE u.id <> $creator_user_id
        RETURN u
        SKIP $skip LIMIT $limit
        """
        result = await session.run(paginated_query, org_id=creator_org_id,creator_user_id=creator_user_id, skip=skip, limit=limit)
        users = [record["u"] for record in await result.data()]

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": users
        }

    # Step 3: Regular user - can only view themselves
    if creator_role == "user":
        query = """MATCH (u:users {id: $user_id}) 
        WHERE u.id <> $creator_user_id
        RETURN u"""
        result = await session.run(query, user_id=creator_user_id,creator_user_id=creator_user_id)
        user = await result.single()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "total": 1,
            "skip": 0,
            "limit": 1,
            "items": [user["u"]]
        }

    raise HTTPException(status_code=403, detail="Unauthorized to view users")



# =============================LOG in APIs with access and resfresh token ==========================================================

@router.post("/login")
async def login(user: UserLogin, session: AsyncSession = Depends(get_db)):
    query = "MATCH (u:users {email: $email}) RETURN u"
    result = await session.run(query, email=user.email)
    record = await result.single()

    if not record:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    db_user = record["u"]
    if not verify_password(user.password, db_user["password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token_data = {
        "id": db_user["id"],
        "email": db_user["email"],
        "role": db_user["role"],
        "name":db_user.get("name"),
        "org_id": db_user.get("org_id")
    }

    access_token = create_access_token(token_data)
    refresh_token = create_refresh_token(token_data, remember_me=user.remember_me)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "role":token_data["role"],
        "name":token_data["name"],
        "org_id":token_data["org_id"],
        "token_type": "bearer"
    }
    
    
@router.post("/refresh")
async def refresh_token(body: RefreshTokenRequest):
    payload = decode_token(body.refresh_token)

    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    new_access_token = create_access_token({
        "sub": payload["sub"],
        "email": payload["email"],
        "role": payload["role"]
    })

    return {"access_token": new_access_token, "token_type": "bearer"}