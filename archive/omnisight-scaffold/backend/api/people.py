from fastapi import APIRouter

from backend.services.person_service import person_service

router = APIRouter()


@router.get("/people")
def get_people():
    return person_service.get_people()
