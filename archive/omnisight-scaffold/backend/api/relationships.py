from fastapi import APIRouter

from backend.services.relationship_service import relationship_service

router = APIRouter()


@router.get("/relationships")
def get_relationships():
    return relationship_service.get_relationships()
