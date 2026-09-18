"""Exporting the Reports page as a file.

A report is not one table but several -- the headline totals, the daily trend,
the funnel, and the breakdowns -- so the two formats differ in shape:

* XLSX gets one sheet per section, which is what a spreadsheet is for.
* CSV cannot hold sheets, so the sections are written one after another, each
  under its own title row and separated by a blank line. Every spreadsheet
  opens that correctly, and it keeps the whole report in the single file the
  user asked for rather than a zip of four.

Both are built in memory: a report is a few hundred rows at most, so there is
nothing to stream.
"""

import csv
import io
from collections.abc import Iterable, Iterator
from typing import Literal

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.schemas.reports import ReportSummary

ExportFormat = Literal["csv", "xlsx"]

CONTENT_TYPES: dict[str, str] = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# A section of the report: a sheet in XLSX, a titled block in CSV.
Section = tuple[str, tuple[str, ...], list[list[object]]]


def _spend_label(kind: str) -> str:
    return {"usage": "Searches & AI expansion", "adjustment": "Adjustments"}.get(
        kind, kind
    )


def _funnel_label(key: str) -> str:
    return {
        "found": "Companies found",
        "in_leads": "Added to My Leads",
        "contacted": "Contacted",
        "interested": "Interested",
    }.get(key, key)


def sections_for(report: ReportSummary) -> list[Section]:
    """Every table in the report, in the order the page shows them."""
    totals = report.totals
    outcomes = report.outcomes

    summary_rows: list[list[object]] = [
        ["Period", f"{report.start_date} to {report.end_date}"],
        ["Days", report.days],
        ["Leads added", totals.leads_added],
        ["Contacts found", totals.contacts_found],
        ["Searches run", totals.searches_run],
        ["Credits spent", totals.credits_spent],
        ["Contacts with a verified email", totals.contacts_with_email],
        ["Contacts with a phone number", totals.contacts_with_phone],
        ["Contacts on WhatsApp", totals.contacts_on_whatsapp],
        ["Verified email rate (%)", totals.email_rate],
        ["Contacts per search", totals.contacts_per_search],
        ["Credits per lead", totals.credits_per_lead],
        ["Searches completed", outcomes.completed],
        ["Searches failed", outcomes.failed],
        ["Searches running", outcomes.running],
        ["Search success rate (%)", outcomes.success_rate],
    ]

    return [
        ("Summary", ("Metric", "Value"), summary_rows),
        (
            "Daily trend",
            ("Date", "Leads added", "Contacts found", "Searches run", "Credits spent"),
            [
                [p.date, p.leads, p.contacts, p.searches, p.credits_spent]
                for p in report.trend
            ],
        ),
        (
            "Funnel",
            ("Stage", "Count"),
            [[_funnel_label(stage.key), stage.count] for stage in report.funnel],
        ),
        (
            "Top countries",
            ("Country", "Companies"),
            [[row.label, row.count] for row in report.top_countries],
        ),
        (
            "Top keywords",
            ("Keyword", "Companies"),
            [[row.label, row.count] for row in report.top_keywords],
        ),
        (
            "Credit spend",
            ("Kind", "Credits"),
            [[_spend_label(row.label), row.count] for row in report.spend_by_kind],
        ),
    ]


def _csv_rows(sections: Iterable[Section]) -> Iterator[list[object]]:
    """Each section under its own title, separated by a blank line."""
    for index, (title, headers, rows) in enumerate(sections):
        if index:
            yield []

        yield [title]
        yield list(headers)
        yield from rows


def render_csv(report: ReportSummary) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(_csv_rows(sections_for(report)))

    # utf-8-sig: the BOM is what makes Excel read it as UTF-8.
    return buffer.getvalue().encode("utf-8-sig")


def render_xlsx(report: ReportSummary) -> bytes:
    workbook = Workbook()

    # A fresh workbook opens with one blank sheet; the first section takes it
    # over rather than leaving an empty "Sheet" behind.
    default = workbook.active
    assert isinstance(default, Worksheet)
    workbook.remove(default)

    for title, headers, rows in sections_for(report):
        sheet = workbook.create_sheet(title=title[:31])

        sheet.append(list(headers))
        for cell in sheet[1]:
            cell.font = Font(bold=True)

        for row in rows:
            sheet.append(row)

        sheet.freeze_panes = "A2"

        for index, label in enumerate(headers, start=1):
            wide = label in {"Metric", "Country", "Keyword", "Kind", "Stage"}
            sheet.column_dimensions[get_column_letter(index)].width = 34 if wide else 16

    buffer = io.BytesIO()
    workbook.save(buffer)

    return buffer.getvalue()


def filename_for(fmt: ExportFormat, report: ReportSummary) -> str:
    """leadpilot-report-2026-08-20-to-2026-09-18.xlsx"""
    return f"leadpilot-report-{report.start_date}-to-{report.end_date}.{fmt}"


def render(report: ReportSummary, fmt: ExportFormat) -> bytes:
    return render_xlsx(report) if fmt == "xlsx" else render_csv(report)


__all__ = [
    "CONTENT_TYPES",
    "ExportFormat",
    "filename_for",
    "render",
    "render_csv",
    "render_xlsx",
    "sections_for",
]
