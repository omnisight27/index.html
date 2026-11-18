from fastapi import APIRouter

from backend.services.project_service import project_service

router = APIRouter()


@router.get("/projects")
def get_projects():
    return project_service.get_projects()
