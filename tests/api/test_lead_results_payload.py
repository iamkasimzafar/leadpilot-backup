"""Ingesting the decision-maker payload the n8n workflow pushes.

The workflow builds these objects in JavaScript:

    companyMap[domain].decision_makers.push({
      full_name, job_title, verified_email, email_status,
      linkedin_url, phone_number, whatsapp_status
    })

where several fields come from `a || b || null` chains and may therefore be
null, missing, over-long, or a JS boolean/number rather than a string. The
results POST is validated as one document, so anything the schema refuses costs
the whole batch -- every company in that run. These tests pin down that nothing
is lost and nothing is rejected.
"""

import json

from app.schemas.lead import CompanyIn, DecisionMakerIn

# --- The documented shape ----------------------------------------------------


def _contact(**overrides: object) -> dict:
    """The exact key set the workflow pushes."""
    base = {
        "full_name": "Jane Roe",
        "job_title": "Head of Procurement",
        "verified_email": "jane@acme.com",
        "email_status": "valid",
        "linkedin_url": "https://www.linkedin.com/in/janeroe",
        "phone_number": "+49 30 1234567",
        "whatsapp_status": "Active",
    }
    base.update(overrides)

    return base


def test_the_documented_payload_round_trips() -> None:
    contact = DecisionMakerIn.model_validate(_contact())

    assert contact.full_name == "Jane Roe"
    assert contact.job_title == "Head of Procurement"
    assert contact.verified_email == "jane@acme.com"
    assert contact.email_status == "valid"
    assert contact.linkedin_url == "https://www.linkedin.com/in/janeroe"
    assert contact.phone_number == "+49 30 1234567"
    assert contact.whatsapp_status == "Active"
    assert contact.model_extra == {}


def test_every_field_may_be_null() -> None:
    """`x || null` yields null for anything the workflow could not find."""
    contact = DecisionMakerIn.model_validate(
        _contact(
            job_title=None,
            verified_email=None,
            linkedin_url=None,
            phone_number=None,
            whatsapp_status=None,
        )
    )

    assert contact.full_name == "Jane Roe"
    assert contact.job_title is None
    assert contact.verified_email is None
    assert contact.whatsapp_status is None


def test_missing_keys_are_accepted() -> None:
    """An `undefined` in JS serialises to a missing key, not to null."""
    contact = DecisionMakerIn.model_validate({"full_name": "Jane Roe"})

    assert contact.job_title is None
    assert contact.whatsapp_status is None


def test_null_whatsapp_means_unchecked_not_inactive() -> None:
    """The workflow sends null when the WhatsApp validator was left off.

    That must stay distinguishable from "Not Active" (checked, no WhatsApp):
    the UI hides the icon for the first and dims it for the second.
    """
    unchecked = DecisionMakerIn.model_validate(_contact(whatsapp_status=None))
    inactive = DecisionMakerIn.model_validate(_contact(whatsapp_status="Not Active"))
    active = DecisionMakerIn.model_validate(_contact(whatsapp_status="Active"))

    assert unchecked.whatsapp_status is None
    assert inactive.whatsapp_status == "Not Active"
    assert active.whatsapp_status == "Active"


def test_placeholder_values_become_null() -> None:
    """"N/A" must not reach the UI as if it were a real value."""
    contact = DecisionMakerIn.model_validate(
        _contact(job_title="N/A", whatsapp_status="-", phone_number="unknown")
    )

    assert contact.job_title is None
    assert contact.whatsapp_status is None
    assert contact.phone_number is None


# --- JS types that are not strings -------------------------------------------


def test_boolean_whatsapp_status_is_coerced() -> None:
    """A JS `true` would otherwise fail the whole batch."""
    assert DecisionMakerIn.model_validate(
        _contact(whatsapp_status=True)
    ).whatsapp_status == "Active"

    assert DecisionMakerIn.model_validate(
        _contact(whatsapp_status=False)
    ).whatsapp_status == "Not Active"


def test_numeric_phone_number_is_coerced() -> None:
    """An unquoted phone number arrives as a JS number."""
    contact = DecisionMakerIn.model_validate(_contact(phone_number=4930123456789))

    assert contact.phone_number == "4930123456789"


def test_a_float_does_not_gain_a_decimal_point() -> None:
    contact = DecisionMakerIn.model_validate(_contact(phone_number=4930123456789.0))

    assert contact.phone_number == "4930123456789"


# --- Over-long values --------------------------------------------------------


def test_long_linkedin_url_is_trimmed_not_rejected() -> None:
    """`source_page` can be far longer than a linkedin.com/in/ URL."""
    url = "https://example.com/team/" + "a" * 600
    contact = DecisionMakerIn.model_validate(_contact(linkedin_url=url))

    assert len(contact.linkedin_url or "") == 500
    # The full value survives, so nothing is actually lost.
    assert (contact.model_extra or {})["linkedin_url_full"] == url


def test_long_job_title_is_trimmed_and_preserved() -> None:
    title = "Global Head of Strategic Procurement and Supply Chain " * 10
    contact = DecisionMakerIn.model_validate(_contact(job_title=title))

    assert len(contact.job_title or "") == 255
    assert (contact.model_extra or {})["job_title_full"] == " ".join(title.split())


def test_long_whatsapp_status_is_trimmed() -> None:
    status = "Active on WhatsApp Business with verified display name"
    contact = DecisionMakerIn.model_validate(_contact(whatsapp_status=status))

    assert len(contact.whatsapp_status or "") == 32
    assert (contact.model_extra or {})["whatsapp_status_full"] == status


def test_values_within_the_limit_are_not_copied() -> None:
    """No `_full` key unless something was actually trimmed."""
    contact = DecisionMakerIn.model_validate(_contact())

    assert contact.model_extra == {}


# --- Unknown fields ----------------------------------------------------------


def test_unknown_keys_are_preserved() -> None:
    """Adding a field in n8n must never lose data or break ingestion."""
    contact = DecisionMakerIn.model_validate(
        _contact(source_page="https://acme.com/team", seniority="C-Level", score=87)
    )

    extras = contact.model_extra or {}
    assert extras["source_page"] == "https://acme.com/team"
    assert extras["seniority"] == "C-Level"
    assert extras["score"] == 87


def test_extras_survive_json_serialisation() -> None:
    """extra_json is written with json.dumps, so extras must be serialisable."""
    contact = DecisionMakerIn.model_validate(
        _contact(source_page="https://acme.com/team", score=87, flagged=True)
    )

    encoded = json.dumps(contact.model_extra, ensure_ascii=False, default=str)

    assert json.loads(encoded)["score"] == 87


# --- The batch must survive one odd contact ----------------------------------


def test_one_odd_contact_does_not_lose_the_company() -> None:
    company = CompanyIn.model_validate(
        {
            "company_name": "Acme GmbH",
            "website": "acme.com",
            "decision_makers": [
                _contact(full_name="Good One"),
                _contact(full_name="Odd One", whatsapp_status=True, phone_number=49301),
                _contact(full_name="Third One", job_title="Director " * 40),
            ],
        }
    )

    assert len(company.decision_makers) == 3
    assert company.decision_makers[1].whatsapp_status == "Active"
    assert company.decision_makers[1].phone_number == "49301"
    assert len(company.decision_makers[2].job_title or "") == 255


def test_company_fields_are_fitted_too() -> None:
    company = CompanyIn.model_validate(
        {
            "company_name": "Acme " * 100,
            "website": "https://acme.com/" + "b" * 600,
            "hq_phone": 4930999,
            "industry": "N/A",
        }
    )

    # <= rather than ==: trimming can land mid-token, and the trailing space is
    # then stripped, so the result is at most the limit.
    assert len(company.company_name) <= 255
    assert (company.model_extra or {})["company_name_full"].startswith("Acme Acme")
    assert len(company.website or "") == 500
    assert company.hq_phone == "4930999"
    assert company.industry is None
    assert (company.model_extra or {})["website_full"].startswith("https://acme.com/")
