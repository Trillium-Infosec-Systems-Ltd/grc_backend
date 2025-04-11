from fastapi import FastAPI, APIRouter

app = FastAPI()
router = APIRouter()

@router.get("/health")
def health_check():
    return {"status": "ok"}

app.include_router(router)