from fastapi import FastAPI, APIRouter
from services.neo4j_client import get_neo4j_driver
from routes import db_CRUD, health,schemas,generics  # Separate routers

app = FastAPI()

# Include all routers
app.include_router(health.router,prefix="/api")
app.include_router(db_CRUD.router,prefix="/api")
app.include_router(schemas.router,prefix="/api")
app.include_router(generics.router,prefix="/api")
for route in app.routes:
    print(route.path)

# Optional: Add shutdown handler
@app.on_event("shutdown")
def shutdown():
    driver = get_neo4j_driver()
    driver.close()