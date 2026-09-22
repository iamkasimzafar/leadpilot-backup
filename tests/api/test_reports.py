"""The reports summary: every figure must be a real count of the user's rows.

The page these back used to chart "emails sent" and "reply rate", which this
product has no data for. These tests pin what replaced them: counts, spend and
a funnel that all come from rows the user owns.
"""

import io
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.lead import Company
from app.services.billing_catalog import BASE_CONTACT_CREDIT
from tests.api.test_auth import PREFIX as AUTH_PREFIX
from tests.api.test_auth import register, signed_in_tokens, verify
from tests.api.test_dashboard import _company, _mock_n8n

PREFIX = settings.API_V1_PREFIX
SUMMARY = f"{PREFIX}/reports/summary"

OTHER = {"email": "other@leadpilot.io", "password": "an0ther-secret-pw"}


@pytest.fixture(autouse=True)
def _webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")


async def _headers(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)

    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _subscribe(client: AsyncClient, headers: dict[str, str]) -> None:
    """Fund the wallet: the start gate refuses a search it could not cover."""
    response = await client.post(
        f"{PREFIX}/billing/subscriptions",
        json={"plan_code": "pro_yearly"},
        headers=headers,
    )
    assert response.status_code == 201


async def _stranger(client: AsyncClient, db_session: Any) -> dict[str, str]:
    await register(client, **OTHER, full_name="Someone Else")
    await verify(client, db_session, email=OTHER["email"])

    response = await client.post(f"{AUTH_PREFIX}/login", json=OTHER)
    assert response.status_code == 200

    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _search_with_results(
    client: AsyncClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    keyword: str,
    companies: list[dict[str, Any]],
    auto_add: bool = True,
) -> str:
    captured = _mock_n8n(monkeypatch)
    started = await client.post(
        f"{PREFIX}/lead-radar/search",
        json={
            "original_keyword": keyword,
            "expanded_keywords": [],
            "auto_add_to_leads": auto_add,
        },
        headers=headers,
    )
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]

    posted = await client.post(
        f"{PREFIX}/lead-radar/results",
        json={"run_id": run_id, "status": "completed", "companies": companies},
        headers={"X-LeadPilot-Run-Token": captured["progress_token"]},
    )
    assert posted.status_code == 201, posted.text

    return run_id


async def _summary(
    client: AsyncClient, headers: dict[str, str], **params: Any
) -> dict[str, Any]:
    response = await client.get(SUMMARY, headers=headers, params=params)
    assert response.status_code == 200, response.text

    return response.json()


# --- Empty state -------------------------------------------------------------


async def test_a_fresh_account_reports_zeros_and_says_it_is_empty(
    client: AsyncClient, db_session: Any
) -> None:
    """No data must read as "nothing yet", not as a set of zeroed charts that
    look like a failure."""
    headers = await _headers(client, db_session)

    body = await _summary(client, headers)

    assert body["is_empty"] is True
    assert body["totals"]["leads_added"] == 0
    assert body["totals"]["contacts_found"] == 0
    assert body["totals"]["credits_spent"] == 0
    assert body["top_countries"] == []
    assert body["top_keywords"] == []
    # The trend still spans the window, so the chart has an axis to draw.
    assert len(body["trend"]) == 30
    assert all(point["leads"] == 0 for point in body["trend"])


# --- Real figures ------------------------------------------------------------


async def test_figures_are_real_counts_of_the_users_rows(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[
            _company("Alpha", "https://alpha.test", 2),
            _company("Beta", "https://beta.test", 1),
        ],
    )

    body = await _summary(client, headers)

    assert body["is_empty"] is False

    totals = body["totals"]
    assert totals["leads_added"] == 2
    assert totals["contacts_found"] == 3
    assert totals["searches_run"] == 1
    # Every contact in the fixture carries a verified email.
    assert totals["contacts_with_email"] == 3
    assert totals["email_rate"] == 100.0
    assert totals["contacts_per_search"] == 3.0

    # Settled from what actually came back: 10 credits per verified-email
    # contact, no run fee or company charge.
    expected_spend = 3 * BASE_CONTACT_CREDIT
    assert totals["credits_spent"] == expected_spend
    assert totals["credits_per_lead"] == round(expected_spend / 2, 1)


async def test_today_appears_on_the_last_day_of_the_trend(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window ends with today, so today's activity must land on the final
    point rather than falling outside the range."""
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[_company("Alpha", "https://alpha.test", 1)],
    )

    body = await _summary(client, headers, days=7)

    assert len(body["trend"]) == 7
    assert body["trend"][-1]["date"] == body["end_date"]
    assert body["trend"][-1]["leads"] == 1
    assert body["trend"][-1]["searches"] == 1
    assert sum(point["leads"] for point in body["trend"]) == 1


async def test_the_funnel_only_ever_narrows(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each stage is a subset of the one before it. Marking a lead interested
    must also count it as contacted -- you cannot skip the step."""
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[
            _company("Alpha", "https://alpha.test", 1),
            _company("Beta", "https://beta.test", 1),
            _company("Gamma", "https://gamma.test", 1),
        ],
    )

    leads = (await client.get(f"{PREFIX}/leads", headers=headers)).json()["items"]
    await client.patch(
        f"{PREFIX}/leads/{leads[0]['id']}", json={"status": "contacted"}, headers=headers
    )
    await client.patch(
        f"{PREFIX}/leads/{leads[1]['id']}",
        json={"status": "interested"},
        headers=headers,
    )

    body = await _summary(client, headers)
    funnel = {stage["key"]: stage["count"] for stage in body["funnel"]}

    assert funnel["found"] == 3
    assert funnel["in_leads"] == 3
    # One contacted plus one interested: the interested lead counts as both.
    assert funnel["contacted"] == 2
    assert funnel["interested"] == 1

    counts = [
        funnel["found"],
        funnel["in_leads"],
        funnel["contacted"],
        funnel["interested"],
    ]
    assert counts == sorted(counts, reverse=True)


async def test_results_not_added_to_leads_are_found_but_not_leads(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-add off means the companies exist but were never taken up, which is
    exactly the gap the funnel's first two bars are there to show."""
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[_company("Alpha", "https://alpha.test", 1)],
        auto_add=False,
    )

    body = await _summary(client, headers)
    funnel = {stage["key"]: stage["count"] for stage in body["funnel"]}

    assert funnel["found"] == 1
    assert funnel["in_leads"] == 0
    assert body["totals"]["leads_added"] == 0
    # The contact was still discovered, and still cost credits.
    assert body["totals"]["contacts_found"] == 1


async def test_top_countries_fold_city_and_country_together(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locations arrive as free text from the workflow. Two cities in one
    country must add up to that country rather than splitting the chart."""
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    companies = [
        {**_company("Alpha", "https://alpha.test", 1), "location": "Hamburg, Germany"},
        {**_company("Beta", "https://beta.test", 1), "location": "Munich, Germany"},
        {**_company("Gamma", "https://gamma.test", 1), "location": "France"},
    ]
    await _search_with_results(
        client, headers, monkeypatch, keyword="LED screen", companies=companies
    )

    countries = (await _summary(client, headers))["top_countries"]

    assert countries[0] == {"label": "Germany", "count": 2}
    assert {"label": "France", "count": 1} in countries


async def test_top_keywords_rank_by_companies_found(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="thin keyword",
        companies=[_company("Solo", "https://solo.test", 1)],
    )
    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="rich keyword",
        companies=[
            _company("Alpha", "https://alpha.test", 1),
            _company("Beta", "https://beta.test", 1),
        ],
    )

    keywords = (await _summary(client, headers))["top_keywords"]

    assert keywords[0]["label"] == "rich keyword"
    assert keywords[0]["count"] == 2
    assert keywords[1]["label"] == "thin keyword"


async def test_spend_counts_usage_and_never_top_ups(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Buying credits is money in. If it leaked into the spend line, topping up
    would look like activity."""
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[_company("Alpha", "https://alpha.test", 1)],
    )

    spent_before = (await _summary(client, headers))["totals"]["credits_spent"]

    topped = await client.post(
        f"{PREFIX}/billing/top-ups", json={"pack_code": "basic"}, headers=headers
    )
    assert topped.status_code == 201

    body = await _summary(client, headers)

    assert body["totals"]["credits_spent"] == spent_before
    assert [slice_["label"] for slice_ in body["spend_by_kind"]] == ["usage"]
    assert body["spend_by_kind"][0]["count"] == spent_before


async def test_outcomes_count_completed_and_failed_searches(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    captured = _mock_n8n(monkeypatch)
    started = await client.post(
        f"{PREFIX}/lead-radar/search",
        json={"original_keyword": "doomed", "expanded_keywords": []},
        headers=headers,
    )
    assert started.status_code == 200
    failed = await client.post(
        f"{PREFIX}/lead-radar/runs/{started.json()['run_id']}/progress",
        json={"stage": "searching_companies", "error": "upstream exploded"},
        headers={"X-LeadPilot-Run-Token": captured["progress_token"]},
    )
    assert failed.status_code == 202

    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[_company("Alpha", "https://alpha.test", 1)],
    )

    outcomes = (await _summary(client, headers))["outcomes"]

    assert outcomes["completed"] == 1
    assert outcomes["failed"] == 1
    assert outcomes["success_rate"] == 50.0


# --- Scope and validation ----------------------------------------------------


async def test_figures_are_scoped_to_the_signed_in_user(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)
    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[_company("Alpha", "https://alpha.test", 2)],
    )

    stranger = await _stranger(client, db_session)
    body = await _summary(client, stranger)

    assert body["is_empty"] is True
    assert body["totals"]["contacts_found"] == 0
    assert body["funnel"][0]["count"] == 0


async def test_activity_outside_the_window_is_excluded(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 7-day window must not count a lead added two months ago."""
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)
    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[_company("Alpha", "https://alpha.test", 1)],
    )

    # Backdate the lead well outside any window the page offers.
    result = await db_session.execute(select(Company))
    company = result.scalars().first()
    company.added_to_leads_at = datetime.now(UTC) - timedelta(days=60)
    await db_session.commit()

    week = await _summary(client, headers, days=7)
    assert week["totals"]["leads_added"] == 0

    year = await _summary(client, headers, days=365)
    assert year["totals"]["leads_added"] == 1


async def test_the_window_length_is_bounded(
    client: AsyncClient, db_session: Any
) -> None:
    """One request must not be able to ask for an unbounded rollup."""
    headers = await _headers(client, db_session)

    too_long = await client.get(SUMMARY, headers=headers, params={"days": 5000})
    assert too_long.status_code == 422

    too_short = await client.get(SUMMARY, headers=headers, params={"days": 0})
    assert too_short.status_code == 422


async def test_summary_requires_auth(client: AsyncClient) -> None:
    response = await client.get(SUMMARY)

    assert response.status_code == 401


# --- Export ------------------------------------------------------------------


async def test_csv_export_contains_every_section(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)
    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[
            {**_company("Alpha", "https://alpha.test", 2), "location": "Hamburg, Germany"}
        ],
    )

    response = await client.get(
        f"{PREFIX}/reports/export", headers=headers, params={"format": "csv"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert ".csv" in response.headers["content-disposition"]

    # The BOM is what makes Excel read it as UTF-8.
    assert response.content.startswith(b"\xef\xbb\xbf")

    text = response.content.decode("utf-8-sig")
    for section in (
        "Summary",
        "Daily trend",
        "Funnel",
        "Top countries",
        "Top keywords",
        "Credit spend",
    ):
        assert section in text, f"{section} missing from the CSV"

    # And the figures themselves, not just the headings.
    assert "Germany" in text
    assert "LED screen" in text


async def test_export_matches_the_summary_it_was_built_from(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file must never disagree with the page."""
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)
    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[
            _company("Alpha", "https://alpha.test", 2),
            _company("Beta", "https://beta.test", 1),
        ],
    )

    body = await _summary(client, headers, days=30)
    response = await client.get(
        f"{PREFIX}/reports/export",
        headers=headers,
        params={"format": "csv", "days": 30},
    )
    text = response.content.decode("utf-8-sig")

    assert f"Leads added,{body['totals']['leads_added']}" in text
    assert f"Contacts found,{body['totals']['contacts_found']}" in text
    assert f"Credits spent,{body['totals']['credits_spent']}" in text
    assert f"{body['start_date']} to {body['end_date']}" in text


async def test_xlsx_export_has_one_sheet_per_section(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)
    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[_company("Alpha", "https://alpha.test", 1)],
    )

    response = await client.get(
        f"{PREFIX}/reports/export", headers=headers, params={"format": "xlsx"}
    )

    assert response.status_code == 200
    assert ".xlsx" in response.headers["content-disposition"]

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(response.content))

    assert workbook.sheetnames == [
        "Summary",
        "Daily trend",
        "Funnel",
        "Top countries",
        "Top keywords",
        "Credit spend",
    ]

    # The trend sheet holds one row per day plus its header.
    trend = workbook["Daily trend"]
    assert trend.max_row == 30 + 1
    assert [cell.value for cell in trend[1]] == [
        "Date",
        "Leads added",
        "Contacts found",
        "Searches run",
        "Credits spent",
    ]


async def test_the_export_window_follows_the_days_asked_for(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _headers(client, db_session)

    response = await client.get(
        f"{PREFIX}/reports/export",
        headers=headers,
        params={"format": "csv", "days": 7},
    )

    assert response.status_code == 200
    text = response.content.decode("utf-8-sig")
    assert "Days,7" in text


async def test_an_unknown_export_format_is_refused(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _headers(client, db_session)

    response = await client.get(
        f"{PREFIX}/reports/export", headers=headers, params={"format": "pdf"}
    )

    assert response.status_code == 422


async def test_export_requires_auth(client: AsyncClient) -> None:
    response = await client.get(f"{PREFIX}/reports/export")

    assert response.status_code == 401
