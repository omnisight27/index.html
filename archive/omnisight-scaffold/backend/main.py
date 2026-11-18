from fastapi import FastAPI
from backend.database import database

from backend.api import (
    documents,
    memories,
    organizations,
    people,
    projects,
    relationships,
)

app = FastAPI(
    title="OmniSight",
    version="0.1.0"
)

app.include_router(people.router)
app.include_router(organizations.router)
app.include_router(projects.router)
app.include_router(documents.router)
app.include_router(memories.router)
app.include_router(relationships.router)


@app.get("/")
def root():
    return {
        "message": "Welcome to OmniSight"
    }


@app.get("/health")
def health():
    return database.health()


@app.get("/tables")
def tables():
    return database.tables()
