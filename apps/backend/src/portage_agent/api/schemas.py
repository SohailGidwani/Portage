"""Pydantic request/response models for the API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class JobCreate(BaseModel):
    repo_url: str = Field(..., examples=["https://github.com/acme/widgets"])
    migration_recipe: str = Field("pydantic_v1_to_v2")
    config: dict = Field(default_factory=dict)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repo_url: str
    migration_recipe: str
    status: str
    config: dict
    error: str | None = None
    report_path: str | None = None
    test_summary: dict | None = None
    graph_summary: dict | None = None
    created_at: datetime
    updated_at: datetime
