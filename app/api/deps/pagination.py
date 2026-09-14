"""Shared pagination query parameters."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query


@dataclass(slots=True)
class PaginationParams:
    page: int
    per_page: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    @property
    def limit(self) -> int:
        return self.per_page


def pagination_params(
    page: Annotated[int, Query(ge=1, description="1-based page number.")] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginationParams:
    return PaginationParams(page=page, per_page=per_page)


Pagination = Annotated[PaginationParams, Depends(pagination_params)]
