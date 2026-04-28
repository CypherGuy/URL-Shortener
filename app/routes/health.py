from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def root() -> dict[str, str]:
    return {"status": "ok"}
