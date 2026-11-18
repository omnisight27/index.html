from fastapi import APIRouter

from backend.services.organization_service import organization_service

router = APIRouter()


@router.get("/organizations")
def get_organizations():
    return organization_service.get_organizations()
