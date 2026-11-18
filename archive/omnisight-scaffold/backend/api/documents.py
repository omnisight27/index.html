from fastapi import APIRouter

from backend.services.document_service import document_service

router = APIRouter()


@router.get("/documents")
def get_documents():
    return document_service.get_documents()
