from fastapi import APIRouter

from backend.services.memory_service import memory_service

router = APIRouter()


@router.get("/memories")
def get_memories():
    return memory_service.get_memories()
