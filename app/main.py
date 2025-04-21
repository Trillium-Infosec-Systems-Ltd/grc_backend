from fastapi import FastAPI, APIRouter
from services.neo4j_client import get_neo4j_driver
from routes import db_CRUD, health,schemas  # Separate routers

app = FastAPI()

# Include all routers
app.include_router(health.router)
app.include_router(db_CRUD.router)
app.include_router(schemas.router)
for route in app.routes:
    print(route.path)

# Optional: Add shutdown handler
@app.on_event("shutdown")
def shutdown():
    driver = get_neo4j_driver()
    driver.close()