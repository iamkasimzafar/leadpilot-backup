"""Schemas shared across resources."""

from math import ceil
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class BaseSchema(BaseModel):
    """Base for response models read from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class Message(BaseModel):
    message: str


class Page(BaseModel, Generic[T]):
    """Envelope for paginated list endpoints."""

    items: list[T]
    total: int = Field(description="Total matching rows, ignoring pagination.")
    page: int
    per_page: int
    pages: int

    @classmethod
    def create(cls, items: list[T], total: int, page: int, per_page: int) -> "Page[T]":
        return cls(
            items=items,
            total=total,
            page=page,
            per_page=per_page,
            pages=ceil(total / per_page) if per_page else 0,
        )
