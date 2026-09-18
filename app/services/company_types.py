"""Company types a lead search can target.

The type answers "what kind of company should we find", which shapes the search
in two places: it is forwarded to the n8n workflow, and it is fed to DeepSeek so
the expanded terms describe how that kind of buyer actually searches. A
distributor and an installer use very different language for the same product.

Kept in step with src/@core/utils/companyTypes.ts on the frontend. Validation
lives here so a hand-crafted request cannot push an arbitrary string into either
the workflow payload or the model prompt.
"""

from typing import NamedTuple


class CompanyType(NamedTuple):
    code: str
    name: str

    # One line of guidance handed to DeepSeek, describing who this company is
    # and what they search for. Written as prompt input, not as UI copy.
    prompt_hint: str


COMPANY_TYPES: dict[str, CompanyType] = {
    "distributor": CompanyType(
        code="distributor",
        name="Distributor / Wholesaler",
        prompt_hint=(
            "companies that buy in bulk and resell to trade customers: distributors, "
            "wholesalers, importers, trading companies and stockists. They search "
            "using trade and supply terms such as 'wholesale', 'bulk supplier', "
            "'distributor', 'import', and product category names with MOQ or "
            "pricing intent."
        ),
    ),
    "brand_oem": CompanyType(
        code="brand_oem",
        name="Brand / OEM",
        prompt_hint=(
            "brand owners and original equipment manufacturers who buy the product "
            "as a component, or have it made to their own specification. They search "
            "using manufacturing and sourcing terms such as 'OEM', 'ODM', 'custom "
            "manufacturer', 'private label', 'contract manufacturing' and technical "
            "component specifications."
        ),
    ),
    "retailer": CompanyType(
        code="retailer",
        name="Retailer",
        prompt_hint=(
            "shops, chains and e-commerce sellers who buy to sell on to end "
            "consumers. They search using retail and merchandising terms such as "
            "'supplier for retail', 'dropshipping', 'store supplier', 'resale', and "
            "consumer-facing product names."
        ),
    ),
    "installer": CompanyType(
        code="installer",
        name="Service / Installer",
        prompt_hint=(
            "contractors, integrators, installers and maintenance firms who buy the "
            "product to fit, integrate or service it for their own clients. They "
            "search using project and service terms such as 'installation', "
            "'system integrator', 'contractor', 'commissioning', 'maintenance' and "
            "application or site names."
        ),
    ),
}


def is_valid(code: str) -> bool:
    """Whether this is a company type the search workflow can use."""
    return code in COMPANY_TYPES


def name_for(code: str) -> str | None:
    """Display name for a code, or None when the code is unknown."""
    entry = COMPANY_TYPES.get(code)

    return entry.name if entry else None


def prompt_hint_for(code: str) -> str | None:
    """The line describing this buyer to DeepSeek, or None for an unknown code."""
    entry = COMPANY_TYPES.get(code)

    return entry.prompt_hint if entry else None


__all__ = ["COMPANY_TYPES", "CompanyType", "is_valid", "name_for", "prompt_hint_for"]
