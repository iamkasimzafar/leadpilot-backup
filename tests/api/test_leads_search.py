"""The My Leads search box: a database search over every lead, not one page."""

from typing import Any

import pytest
from httpx import AsyncClient

from app.core.config import settings
from tests.api.test_leads import LEADS, _leads, _run_search

PREFIX = settings.API_V1_PREFIX


@pytest.fixture(autouse=True)
def _webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")


def _names(page: dict) -> set[str]:
    return {lead["company_name"] for lead in page["items"]}


async def _three_leads(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """Alpha Ltd, Beta GmbH and Gamma Inc, all in My Leads."""
    headers, _ = await _run_search(client, db_session, monkeypatch, auto_add=True)
    return headers


async def test_search_reaches_leads_that_are_not_on_the_open_page(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: with one lead per page, a lead on page 2 or 3 is still
    found from page 1, and the total describes the matches, not the list."""
    headers = await _three_leads(client, db_session, monkeypatch)

    unfiltered = await _leads(client, headers, page=1, per_page=1)
    assert unfiltered["total"] == 3
    assert len(unfiltered["items"]) == 1

    for name in ("Alpha Ltd", "Beta GmbH", "Gamma Inc"):
        found = await _leads(client, headers, page=1, per_page=1, q=name.split()[0])
        assert _names(found) == {name}
        assert found["total"] == 1


async def test_search_is_case_insensitive_and_matches_part_of_a_word(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _three_leads(client, db_session, monkeypatch)

    assert _names(await _leads(client, headers, q="gAmM")) == {"Gamma Inc"}
    assert _names(await _leads(client, headers, q="  beta  ")) == {"Beta GmbH"}


async def test_search_covers_the_website_and_other_company_fields(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _three_leads(client, db_session, monkeypatch)

    assert _names(await _leads(client, headers, q="alpha.test")) == {"Alpha Ltd"}
    # Every company here is in Manufacturing.
    assert (await _leads(client, headers, q="manufactur"))["total"] == 3


async def test_search_finds_a_lead_by_one_of_its_contacts(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _three_leads(client, db_session, monkeypatch)

    by_email = await _leads(client, headers, q="p2@gamma.test")
    assert _names(by_email) == {"Gamma Inc"}

    by_name = await _leads(client, headers, q="Beta GmbH Person 0")
    assert _names(by_name) == {"Beta GmbH"}


async def test_a_lead_with_several_matching_contacts_is_listed_once(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gamma has three contacts that all match "gamma.test". Joining to the
    contacts would return it three times and report a total of three."""
    headers = await _three_leads(client, db_session, monkeypatch)

    found = await _leads(client, headers, q="@gamma.test")

    assert [lead["company_name"] for lead in found["items"]] == ["Gamma Inc"]
    assert found["total"] == 1
    assert found["counts"]["all"] == 1


async def test_every_word_must_match_but_not_in_the_same_field(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _three_leads(client, db_session, monkeypatch)

    # "alpha" is in the company name, "buyer" is a contact's job title.
    assert _names(await _leads(client, headers, q="alpha buyer")) == {"Alpha Ltd"}
    # "alpha" and "gamma" never describe the same lead.
    assert (await _leads(client, headers, q="alpha gamma"))["total"] == 0


async def test_wildcard_characters_are_searched_as_text(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare % must not match everything."""
    headers = await _three_leads(client, db_session, monkeypatch)

    assert (await _leads(client, headers, q="%"))["total"] == 0
    assert (await _leads(client, headers, q="_"))["total"] == 0

    lead_id = (await _leads(client, headers, q="alpha"))["items"][0]["id"]
    updated = await client.patch(
        f"{LEADS}/{lead_id}", json={"notes": "Asked for a 50% discount"}, headers=headers
    )
    assert updated.status_code == 200

    assert _names(await _leads(client, headers, q="50%")) == {"Alpha Ltd"}


async def test_tab_counts_follow_the_search_and_combine_with_the_tab(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _three_leads(client, db_session, monkeypatch)
    alpha = (await _leads(client, headers, q="alpha"))["items"][0]["id"]
    moved = await client.patch(
        f"{LEADS}/{alpha}", json={"status": "contacted"}, headers=headers
    )
    assert moved.status_code == 200

    everything = await _leads(client, headers, q="test")
    assert everything["counts"] == {"all": 3, "new": 2, "contacted": 1, "interested": 0}

    only_alpha = await _leads(client, headers, q="alpha")
    assert only_alpha["counts"] == {"all": 1, "new": 0, "contacted": 1, "interested": 0}

    # The tab narrows the list; the counts still describe the search alone.
    in_new_tab = await _leads(client, headers, q="alpha", status="new")
    assert in_new_tab["total"] == 0
    assert in_new_tab["counts"]["contacted"] == 1


async def test_a_blank_search_is_no_search(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _three_leads(client, db_session, monkeypatch)

    assert (await _leads(client, headers, q="   "))["total"] == 3
    assert (await _leads(client, headers, q=""))["total"] == 3


async def test_search_never_crosses_accounts(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _three_leads(client, db_session, monkeypatch)

    registered = await client.post(
        f"{PREFIX}/auth/register",
        json={
            "email": "other@leadpilot.io",
            "password": "an0ther-secret",
            "full_name": "Someone Else",
        },
    )
    assert registered.status_code == 201

    from tests.api.test_auth import verify

    await verify(client, db_session, "other@leadpilot.io")
    login = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "other@leadpilot.io", "password": "an0ther-secret"},
    )
    other = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (await _leads(client, other, q="alpha"))["total"] == 0


async def test_the_export_honours_the_search(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What is downloaded is what is on screen: the matches, across all pages."""
    headers = await _three_leads(client, db_session, monkeypatch)

    response = await client.get(
        f"{LEADS}/export", params={"format": "csv", "q": "gamma"}, headers=headers
    )

    assert response.status_code == 200
    body = response.text
    assert "Gamma Inc" in body
    assert "Alpha Ltd" not in body
    assert "Beta GmbH" not in body


async def test_an_overlong_search_is_refused(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _three_leads(client, db_session, monkeypatch)

    response = await client.get(LEADS, params={"q": "x" * 101}, headers=headers)

    assert response.status_code == 422
