"""Transport-independent normalized metadata shared by adapters and services."""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class MaterialLink(BaseModel):
    role: str = "main_text"
    url: str
    format: str = "pdf"


class Candidate(BaseModel):
    source: str
    source_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    abstract: str = ""
    identifiers: dict[str, str] = Field(default_factory=dict)
    version_kind: str = "unknown"
    landing_url: str | None = None
    fulltext_urls: list[str] = Field(default_factory=list)
    links: list[MaterialLink] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class SourcePage(BaseModel):
    items: list[Candidate] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int | None = None
    warnings: list[str] = Field(default_factory=list)


class ResearchError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}


def success(data: Any) -> dict:
    return {"schema_version": 1, "data": data}
