"""Contact role and company size filters for a lead search.

Both narrow what the workflow extracts rather than what it finds:

- `contact_role` is handed to the Snov.io extraction filter, so only the right
  decision-makers are pulled -- and, since extraction is what costs credits,
  so the user is not charged for contacts they did not ask for.
- `company_size` is a Snov.io employee-count band, which that API filters on
  natively, so it is forwarded as the band the API expects.

Kept in step with src/@core/utils/searchTargeting.ts on the frontend.
Validation lives here so a hand-crafted request cannot push an arbitrary string
into the workflow payload.
"""

from typing import NamedTuple


class ContactRole(NamedTuple):
    code: str
    name: str

    # Job-title families Snov.io should match for this role. Sent to the
    # workflow so the extraction filter does not need its own mapping.
    #
    # Capped at MAX_ROLE_TITLES: the n8n engine cannot handle more than ten in
    # one filter, so each list is kept to the titles that actually buy, ordered
    # most-common first.
    titles: tuple[str, ...]


class CompanySize(NamedTuple):
    code: str
    name: str

    # Employee-count bounds. `max_employees` is None for the open-ended band.
    min_employees: int
    max_employees: int | None


# The n8n workflow's extraction filter cannot take more than ten job titles, so
# every role's list stays under this. Enforced by the assertion below.
MAX_ROLE_TITLES = 9


# "any" is deliberately absent: it is the default, and it means "send no
# filter", which is represented as None rather than as a code.
CONTACT_ROLES: dict[str, ContactRole] = {
    "c_level": ContactRole(
        code="c_level",
        name="C-Level / Owner",
        # CTO / COO / CFO were dropped to fit the cap: they are the least likely
        # of the C-suite to be the buyer for a product enquiry, and the titles
        # left cover the owner-operator this role is named for.
        titles=(
            "CEO",
            "Chief Executive Officer",
            "Founder",
            "Co-Founder",
            "Owner",
            "Managing Director",
            "President",
            "Partner",
            "Chairman",
        ),
    ),
    "procurement": ContactRole(
        code="procurement",
        name="Procurement / Buyer",
        titles=(
            "Procurement Manager",
            "Purchasing Manager",
            "Buyer",
            "Head of Procurement",
            "Sourcing Manager",
            "Supply Chain Manager",
            "Category Manager",
            "Purchasing Director",
        ),
    ),
    "marketing": ContactRole(
        code="marketing",
        name="Marketing",
        titles=(
            "Marketing Manager",
            "Head of Marketing",
            "CMO",
            "Brand Manager",
            "Marketing Director",
            "Digital Marketing Manager",
            "Product Marketing Manager",
        ),
    ),
    "sales": ContactRole(
        code="sales",
        name="Sales",
        titles=(
            "Sales Manager",
            "Head of Sales",
            "Sales Director",
            "Business Development Manager",
            "VP Sales",
            "Account Manager",
            "Sales Executive",
            "Regional Sales Manager",
        ),
    ),
    "engineering": ContactRole(
        code="engineering",
        name="Engineering",
        titles=(
            "Engineering Manager",
            "Head of Engineering",
            "CTO",
            "Technical Director",
            "R&D Manager",
            "Chief Engineer",
            "VP Engineering",
            "Product Engineer",
        ),
    ),
}


# Fails at import rather than mid-search: an over-long list would only show up
# as the workflow silently refusing the filter.
assert all(len(role.titles) <= MAX_ROLE_TITLES for role in CONTACT_ROLES.values()), (
    f"every contact role must have at most {MAX_ROLE_TITLES} job titles"
)


COMPANY_SIZES: dict[str, CompanySize] = {
    "1_10": CompanySize("1_10", "1-10 employees", 1, 10),
    "11_50": CompanySize("11_50", "11-50 employees", 11, 50),
    "51_200": CompanySize("51_200", "51-200 employees", 51, 200),
    "201_plus": CompanySize("201_plus", "200+ employees", 201, None),
}


# --- Contact roles ------------------------------------------------------------
def is_valid_role(code: str) -> bool:
    return code in CONTACT_ROLES


def role_name_for(code: str) -> str | None:
    entry = CONTACT_ROLES.get(code)

    return entry.name if entry else None


def role_titles_for(code: str) -> list[str] | None:
    """The job titles Snov.io should filter on, or None for an unknown code."""
    entry = CONTACT_ROLES.get(code)

    return list(entry.titles) if entry else None


# --- Company sizes ------------------------------------------------------------
def is_valid_size(code: str) -> bool:
    return code in COMPANY_SIZES


def size_name_for(code: str) -> str | None:
    entry = COMPANY_SIZES.get(code)

    return entry.name if entry else None


def size_bounds_for(code: str) -> tuple[int, int | None] | None:
    """(min, max) employee counts, max None when open-ended."""
    entry = COMPANY_SIZES.get(code)

    return (entry.min_employees, entry.max_employees) if entry else None


__all__ = [
    "COMPANY_SIZES",
    "CONTACT_ROLES",
    "MAX_ROLE_TITLES",
    "CompanySize",
    "ContactRole",
    "is_valid_role",
    "is_valid_size",
    "role_name_for",
    "role_titles_for",
    "size_bounds_for",
    "size_name_for",
]
