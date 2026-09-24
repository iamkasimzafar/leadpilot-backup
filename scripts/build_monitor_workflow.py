"""Generate the Radar Monitors n8n workflow from the manual-search one.

The monitor workflow is the same Serper -> DeepSeek -> Snov.io pipeline the
manual search already runs; only its trigger, its Serper paging and its
results callback differ. Deriving it here, rather than hand-maintaining a
second 40-node JSON, means a fix to the shared middle is picked up by
re-running this script.

The workflow has no schedule of its own. Celery (app/tasks/monitors.py) POSTs
one monitor at a time to its Webhook, at whatever random slot that monitor
drew, so n8n never sees more than a trickle. The payload carries the same
field names the manual search's webhook body does (`expanded_keywords`,
`original_keyword`, `country`, ...), which is why the shared nodes need no
rewriting at all.

What changes, node by node:

Webhook  path `radar-monitor-v1` instead of `lead-search-v1`; same shape.

serper   `num` and `page` come from the monitor, so each run digs past the
         results the previous ones already returned. Without that, day two
         re-reads day one's page and finds nothing new.

empty    The six nodes that throw when a stage yields nothing get an error
         output wired straight to the results node. For a monitor, "nothing
         new past page 3" is a quiet day, not a fault: the run still has to
         report back so its offset advances, or it would re-read the same
         dead page every night.

results  Posts to /lead-radar/monitors/results with the monitor's own token.
         The backend dedupes on email there and bills only what is new.

Progress nodes are dropped: a monitor has no live tracker to feed, and the
run's progress token would not authenticate them anyway.

Usage:
    python scripts/build_monitor_workflow.py

Env:
    N8N_MONITOR_WORKFLOW_ID   id of the workflow to overwrite on import
                              (default: the one already on the live n8n)
"""

import json
import os
import pathlib
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "n8n_workflow" / "LeadPilot.json"
TARGET = ROOT / "n8n_workflow" / "LeadPilot - Radar Monitors.json"

# Carrying the id means `n8n import:workflow` replaces the existing workflow
# in place instead of creating "LeadPilot - Radar Monitors (2)" every time.
WORKFLOW_ID = os.environ.get("N8N_MONITOR_WORKFLOW_ID", "KI1AtDZtXlSd3FA5")

WEBHOOK_PATH = "radar-monitor-v1"

# Nodes that only exist to drive the live progress tracker.
PROGRESS_NODES = {
    "searching companies",
    "AI Analysing",
    "Domain Search",
    "Finding Decision Makers",
    "Finding Emails",
    "Verifying Contacts",
}

# Nodes that throw when a stage yields nothing.
#
# In the manual search that is right: a user is watching, and an empty result
# should surface as an error they can read. For a monitor it is wrong twice
# over. "No new companies past page 3" is the ordinary outcome of a monitor
# that has been running a while, not a fault. And the run has to report back
# regardless, so its offset advances past the dead page -- otherwise it would
# re-read the same empty results every night.
#
# `continueErrorOutput` gives each of these a second output that fires
# instead of throwing. It is wired straight to the results node, which sees
# no company items and posts an empty list: a completed run worth zero leads.
EMPTY_RESULT_NODES = {
    "Backlisting",
    "Guard: AI Qualified",
    "Filter the companies",
    "Filter & Limit Prospects",
    "Guard: Valid Emails",
    "Group Leads & Apply Abandon Rule",
}

RESULTS_NODE = "Post Monitor Results"


def build() -> dict:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    by_name = {node["name"]: node for node in source["nodes"]}

    nodes: list[dict] = []

    for node in source["nodes"]:
        if node["name"] in PROGRESS_NODES:
            continue

        clone = json.loads(json.dumps(node))

        if clone["name"] == "Webhook":
            clone["parameters"] = {
                "httpMethod": "POST",
                "path": WEBHOOK_PATH,
                "options": {},
            }
            # A fresh webhook id: two active workflows must not share one.
            clone["webhookId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, WEBHOOK_PATH))
            clone["notes"] = (
                "Called by the LeadPilot backend's Celery dispatcher, one "
                "monitor per call, at that monitor's random daily slot. "
                "There is no schedule in n8n."
            )

        if clone["name"] == "serper":
            clone["parameters"]["jsonBody"] = _serper_body(
                by_name["serper"]["parameters"]["jsonBody"]
            )
            clone["notes"] = (
                "`page` comes from the monitor's stored serper_offset. Without "
                "it every run re-reads the same top results and finds nothing "
                "new from day two on."
            )

        if clone["name"] in EMPTY_RESULT_NODES:
            clone["onError"] = "continueErrorOutput"

        if clone["name"] == "Send Results to Backend":
            clone["name"] = RESULTS_NODE
            clone["parameters"] = _results_params()
            # One POST carrying every company, not one per item.
            clone["executeOnce"] = True
            # Still emit something when there was nothing to post, so the
            # execution shows the node ran rather than looking skipped.
            clone["alwaysOutputData"] = True
            clone["notes"] = (
                "The backend dedupes these contacts against everything the "
                "user already has and bills only what is new. Fed by the "
                "normal path and by every empty-stage error output."
            )

        nodes.append(clone)

    # --- Connections --------------------------------------------------------
    connections: dict = {}

    for name, outputs in source["connections"].items():
        if name in PROGRESS_NODES:
            continue

        kept_outputs = []
        for branch in outputs.get("main", []):
            kept_outputs.append(
                [
                    (
                        {**link, "node": RESULTS_NODE}
                        if link.get("node") == "Send Results to Backend"
                        else link
                    )
                    for link in (branch or [])
                    if link.get("node") not in PROGRESS_NODES
                ]
            )

        connections[name] = {"main": kept_outputs}

    if "Send Results to Backend" in connections:
        connections[RESULTS_NODE] = connections.pop("Send Results to Backend")

    # Note on Switch's `no_email` output, which is left wired on purpose.
    #
    # It feeds the same Merge input as the `valid` output, and n8n does not
    # combine two deliveries to one input: when both carry items, Merge runs
    # twice, Guard: Valid Emails throws on the no-email wave, and its error
    # output fires the results node a second time with an empty list. The
    # backend treats that repeat as a no-op (ingest is idempotent per run:
    # the offset moves once, whichever post lands first).
    #
    # Cutting the link instead would break the one case it exists for: when
    # every prospect has no email, the `valid` and `unknown` branches are
    # both empty, nothing else reaches Merge, and the run would never report
    # back at all.

    # Every empty-stage node's error output goes straight to the results
    # node. Output 0 is the node's normal path; `continueErrorOutput` adds
    # output 1.
    for name in EMPTY_RESULT_NODES:
        outputs = connections.setdefault(name, {"main": [[]]})["main"]
        while len(outputs) < 2:
            outputs.append([])
        outputs[1].append({"node": RESULTS_NODE, "type": "main", "index": 0})

    # --- Settings -----------------------------------------------------------
    settings = dict(source.get("settings", {}))

    # The inherited error workflow reports to /lead-radar/error, which
    # attributes a failure to the lead_search_run in flight. A monitor's own
    # run is closed by its results callback, and an unrelated manual search
    # must never be blamed for a monitor's failure. n8n's execution list still
    # records anything that goes wrong.
    settings.pop("errorWorkflow", None)

    return {
        "id": WORKFLOW_ID,
        "name": "LeadPilot - Radar Monitors",
        "nodes": nodes,
        "pinData": {},
        "connections": connections,
        "active": False,
        "settings": settings,
        "meta": source.get("meta", {}),
        "tags": [],
    }


def _serper_body(source_body: str) -> str:
    """The manual search's Serper request, asking for the monitor's page.

    Everything else -- the keyword, the company-type OR-group, the country --
    is left exactly as the manual search builds it, so the two stay in step.
    `num` must match the page width the backend assumed when it turned
    serper_offset into serper_page, or the monitor would re-read ground it has
    already covered; the backend caps limit_per_run at 100, which is also
    Serper's own maximum.
    """
    replacement = (
        "num: Math.min($('Webhook').item.json.body.limit_per_run || 50, 100),\n"
        "    // The whole point of the monitor: start where the last run\n"
        "    // stopped. Page 1 is offset 0, page 2 the next `num` results.\n"
        "    page: $('Webhook').item.json.body.serper_page || 1"
    )

    if "num: 50" not in source_body:
        raise SystemExit(
            "LeadPilot.json's serper node no longer says `num: 50`; update "
            "_serper_body() to match its new shape."
        )

    return source_body.replace("num: 50", replacement, 1)


def _results_params() -> dict:
    """Post the run's companies back against the monitor's own token.

    Items arriving from an empty-stage error output carry an `error` key
    rather than a company, so they are stripped: a quiet run posts an empty
    list, not a malformed company the backend's CompanyIn would reject.
    """
    body = """={{
JSON.stringify((() => {
  const companies = $input.all()
    .map(i => i.json)
    .filter(c => c && !c.error && c.company_name);

  const m = $('Webhook').first().json.body;

  return {
    monitor_id: m.monitor_id,
    run_id: m.run_id,
    status: 'completed',
    companies
  };
})())
}}"""

    return {
        "method": "POST",
        "url": "={{ $('Webhook').first().json.body.results_url }}",
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [
                {
                    "name": "X-LeadPilot-Run-Token",
                    "value": "={{ $('Webhook').first().json.body.progress_token }}",
                }
            ]
        },
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": body,
        "options": {},
    }


if __name__ == "__main__":
    workflow = build()
    TARGET.write_text(
        json.dumps(workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(workflow['nodes'])} nodes)")
    print(f"  webhook path: /webhook/{WEBHOOK_PATH}")
    print(f"  workflow id:  {WORKFLOW_ID} (import replaces it in place)")
