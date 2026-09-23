"""Generate the Radar Monitors n8n workflow from the manual-search one.

The monitor workflow is the same Serper -> DeepSeek -> Snov.io pipeline the
manual search already runs; only its head and tail differ. Deriving it here,
rather than hand-maintaining a second 40-node JSON, means a fix to the shared
middle is picked up by re-running this script.

What changes:

head    The Webhook is replaced by a Schedule Trigger plus an HTTP node that
        asks the backend which monitors are due. The backend claims each one
        as it hands it out, so an overlapping run cannot dispatch twice.

serper  The query comes from the monitor's search term, and the request
        carries the monitor's `page`, so each run digs past the results the
        previous ones already returned. Without that, day two re-reads day
        one's page and finds nothing new.

tail    Results post to /lead-radar/monitors/results with the monitor's own
        callback token. The backend dedupes on email there and bills only what
        is genuinely new.

Progress nodes are dropped: a monitor has no UI watching it.

Usage:  python scripts/build_monitor_workflow.py
"""

import json
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "n8n_workflow" / "LeadPilot.json"
TARGET = ROOT / "n8n_workflow" / "LeadPilot - Radar Monitors.json"

# Where the workflow calls the backend, and the secret it presents.
#
# Inlined rather than read from `$env`, because n8n denies expression access
# to environment variables by default (N8N_BLOCK_ENV_ACCESS_IN_NODE) and this
# instance runs as a hand-started container, so adding vars would mean
# recreating it. Every existing LeadPilot workflow inlines its keys the same
# way; this stays consistent with them.
#
# Overridable so a different host or secret can be generated without editing
# the script:  API_URL=https://... python scripts/build_monitor_workflow.py
API_URL = os.environ.get("LEADPILOT_API_URL", "http://43.157.81.74:8000")
WORKFLOW_SECRET = os.environ.get("LEADPILOT_N8N_SECRET", "")

# Reused from the manual-search workflow so both call Serper on one account.
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")


def _serper_key_from_source(source: dict) -> str:
    """The Serper key the manual-search workflow already uses.

    Read from that workflow rather than duplicated here, so rotating the key
    in one place and regenerating keeps the two in step.
    """
    for node in source["nodes"]:
        if node["name"] != "serper":
            continue

        params = node.get("parameters", {})
        headers = params.get("headerParameters", {}).get("parameters", [])
        for header in headers:
            if header.get("name", "").lower() == "x-api-key":
                return str(header.get("value", ""))

    return ""

# Nodes that only exist to drive the live progress tracker. A monitor runs
# unattended, so there is nothing to report to.
PROGRESS_NODES = {
    "searching companies",
    "AI Analysing",
    "Domain Search",
    "Finding Decision Makers",
    "Finding Emails",
    "Verifying Contacts",
}

# The head we replace.
HEAD_NODES = {"Webhook", "Split Out"}

# Nodes that throw when a stage yields nothing.
#
# In the manual search that is right: a user is watching, and an empty result
# should surface as an error they can read. For a monitor it is wrong twice
# over. "No new companies past page 3" is the ordinary outcome of a monitor
# that has been running a while, not a fault. And an uncaught throw aborts the
# whole execution, so with several monitors in the batch the first barren one
# would strand every monitor queued behind it -- none of them dispatched, none
# of their offsets advanced, and the failure attributed to no monitor at all.
#
# `continueRegularOutput` lets the empty result flow on instead. The run ends
# at Post Monitor Results with an empty company list, which the backend treats
# as a completed run worth zero leads.
EMPTY_RESULT_NODES = {
    "Backlisting",
    "Guard: AI Qualified",
    "Filter the companies",
    "Filter & Limit Prospects",
    "Guard: Valid Emails",
    "Group Leads & Apply Abandon Rule",
}


def _monitor_ref(expr: str) -> str:
    """Rewrite `$('Webhook')...body.X` to read from the current monitor.

    Every field the pipeline needs is carried on the monitor item the
    "Split Monitors" node emits, under the same names the webhook used, so the
    downstream nodes keep working untouched.
    """
    monitor = "$('Loop Monitors').item.json"

    # $('Webhook') and $("Webhook") are both in use.
    expr = expr.replace('$("Webhook")', "$('Webhook')")

    # .json.body.foo and .json.body?.foo, off .item or .first().
    expr = re.sub(
        r"\$\('Webhook'\)\.(?:item|first\(\))\.json\.body\??\.([a-zA-Z_]+)",
        rf"{monitor}.\1",
        expr,
    )

    # A few nodes read .json.foo directly, having already fallen back off body.
    expr = re.sub(
        r"\$\('Webhook'\)\.(?:item|first\(\))\.json\.([a-zA-Z_]+)",
        rf"{monitor}.\1",
        expr,
    )

    # Whole-payload grabs: `const webhookData = $('Webhook').first().json`.
    # The monitor item is already flat, so `body || webhookData` still works.
    expr = re.sub(r"\$\('Webhook'\)\.(?:item|first\(\))\.json", monitor, expr)

    # Bare $json['body.foo'] used by the Split Out consumer.
    expr = expr.replace("$json['body.expanded_keywords']", "$json.search_term")

    return expr


def _rewrite(value: object) -> object:
    if isinstance(value, str):
        return _monitor_ref(value)
    if isinstance(value, list):
        return [_rewrite(item) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item) for key, item in value.items()}

    return value


def build() -> dict:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))

    kept = [
        node
        for node in source["nodes"]
        if node["name"] not in PROGRESS_NODES | HEAD_NODES
    ]

    nodes: list[dict] = []

    # --- Head ---------------------------------------------------------------
    nodes.append(
        {
            "parameters": {
                "rule": {
                    "interval": [
                        {
                            "field": "cronExpression",
                            # 02:00 UTC daily: low-traffic, and far from the
                            # hours people run manual searches.
                            "expression": "0 2 * * *",
                        }
                    ]
                }
            },
            "id": "monitor-schedule-trigger",
            "name": "Daily 02:00 UTC",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [-640, 0],
        }
    )

    nodes.append(
        {
            "parameters": {
                "method": "GET",
                "url": f"{API_URL.rstrip('/')}/api/v1/lead-radar/monitors/due",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {
                            "name": "X-LeadPilot-Token",
                            "value": WORKFLOW_SECRET,
                        }
                    ]
                },
                # A monitor that is not due is not an error: the endpoint
                # returns an empty list, and the run should end quietly.
                "options": {},
            },
            "id": "monitor-fetch-due",
            "name": "Fetch Due Monitors",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [-420, 0],
            "notes": (
                "The backend claims each monitor as it hands it out, so a "
                "retry or an overlapping run cannot dispatch the same monitor "
                "twice and bill the user twice over."
            ),
        }
    )

    nodes.append(
        {
            "parameters": {"fieldToSplitOut": "monitors", "options": {}},
            "id": "monitor-split",
            "name": "Split Monitors",
            "type": "n8n-nodes-base.splitOut",
            "typeVersion": 1,
            "position": [-200, 0],
        }
    )

    # One monitor at a time: each needs its own Serper page and its own
    # results callback, and a shared run would blur them together.
    nodes.append(
        {
            "parameters": {"options": {"reset": False}},
            "id": "monitor-loop",
            "name": "Loop Monitors",
            "type": "n8n-nodes-base.splitInBatches",
            "typeVersion": 3,
            "position": [20, 0],
        }
    )

    # --- Shared middle, with webhook refs rewritten -------------------------
    for node in kept:
        clone = dict(node)
        clone["parameters"] = _rewrite(node.get("parameters", {}))

        if clone["name"] in EMPTY_RESULT_NODES:
            # An empty stage is a normal quiet day for a monitor, not a fault.
            clone["onError"] = "continueRegularOutput"

        if clone["name"] == "serper":
            clone["parameters"] = _serper_params(
                SERPER_API_KEY or _serper_key_from_source(source)
            )
            clone["notes"] = (
                "`page` comes from the monitor's stored serper_offset. Without "
                "it every run re-reads the same top results and finds nothing "
                "new from day two on."
            )

        if clone["name"] == "Send Results to Backend":
            clone["parameters"] = _results_params()
            clone["name"] = "Post Monitor Results"

            # One POST carrying every company, not one per item.
            clone["executeOnce"] = True

            # The loop only advances when this node emits something. Without
            # this, a monitor that found nothing would post nothing, produce
            # no output item, and leave the batch stalled -- every monitor
            # behind it silently skipped for the night.
            clone["alwaysOutputData"] = True

            # A monitor whose backend POST fails must not take the rest of the
            # batch down with it. The loop continues; the backend simply never
            # hears about this one, and its offset stays where it was.
            clone["onError"] = "continueRegularOutput"

            clone["notes"] = (
                "The backend dedupes these contacts against everything the "
                "user already has and bills only what is new."
            )

        nodes.append(clone)

    # --- Connections --------------------------------------------------------
    connections = {
        key: value
        for key, value in source["connections"].items()
        if key not in PROGRESS_NODES | HEAD_NODES
    }

    # Drop links that pointed at a node we removed, and rename the tail node.
    for outputs in connections.values():
        for branch in outputs.get("main", []):
            if branch is None:
                continue
            branch[:] = [
                link
                for link in branch
                if link.get("node") not in PROGRESS_NODES | HEAD_NODES
            ]
            for link in branch:
                if link.get("node") == "Send Results to Backend":
                    link["node"] = "Post Monitor Results"

    if "Send Results to Backend" in connections:
        connections["Post Monitor Results"] = connections.pop("Send Results to Backend")

    # Head wiring, and the loop back for the next monitor.
    connections["Daily 02:00 UTC"] = {
        "main": [[{"node": "Fetch Due Monitors", "type": "main", "index": 0}]]
    }
    connections["Fetch Due Monitors"] = {
        "main": [[{"node": "Split Monitors", "type": "main", "index": 0}]]
    }
    connections["Split Monitors"] = {
        "main": [[{"node": "Loop Monitors", "type": "main", "index": 0}]]
    }
    # Output 0 is "done", output 1 is the per-item branch.
    connections["Loop Monitors"] = {
        "main": [[], [{"node": "serper", "type": "main", "index": 0}]]
    }
    connections["Post Monitor Results"] = {
        "main": [[{"node": "Loop Monitors", "type": "main", "index": 0}]]
    }

    settings = dict(source.get("settings", {}))

    # The inherited error workflow reports to /lead-radar/error, which
    # attributes a failure to a lead_search_run. A monitor has no run, so
    # every monitor failure would be blamed on whichever manual search
    # happened to be in flight. Dropped until there is a monitor-aware
    # handler; failures are visible in n8n's own execution list meanwhile.
    settings.pop("errorWorkflow", None)

    return {
        "name": "LeadPilot - Radar Monitors",
        "nodes": nodes,
        "pinData": {},
        "connections": connections,
        "active": False,
        "settings": settings,
        "meta": source.get("meta", {}),
        "tags": [],
    }


def _serper_params(serper_key: str) -> dict:
    """Serper, asking for the monitor's page rather than always the first."""
    js = """={{
JSON.stringify((() => {
  const m = $('Loop Monitors').item.json;

  // `num` must match the page width the backend assumed when it turned
  // serper_offset into serper_page, or the two drift and the monitor
  // re-reads ground it has already covered. The backend caps limit_per_run
  // at 100, which is also Serper's own maximum.
  const num = Math.min(m.limit_per_run || 50, 100);

  const body = {
    q: m.search_term,
    num,
    // The whole point of the monitor: start where the last run stopped.
    // Page 1 is offset 0, page 2 is the next `num` results, and so on.
    page: m.serper_page || 1
  };

  // Serper reads an empty `gl` as a country filter, not as "worldwide", so
  // the key is left out entirely rather than sent blank.
  if (m.country) body.gl = m.country;

  return body;
})())
}}"""

    return {
        "method": "POST",
        "url": "https://google.serper.dev/search",
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [
                {"name": "X-API-KEY", "value": serper_key},
                {"name": "Content-Type", "value": "application/json"},
            ]
        },
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": js,
        "options": {},
    }


def _results_params() -> dict:
    """Post the run's companies back against the monitor's own token."""
    # Items that reached here after an upstream node was told to continue on
    # error carry an `error` key instead of a company. They are stripped out,
    # so a quiet run posts an empty list rather than a malformed company the
    # backend's CompanyIn would reject.
    body = """={{
JSON.stringify((() => {
  const companies = $input.all()
    .map(i => i.json)
    .filter(c => c && !c.error && c.company_name);

  return {
    monitor_id: $('Loop Monitors').item.json.monitor_id,
    run_id: $('Loop Monitors').item.json.run_id,
    status: 'completed',
    companies
  };
})())
}}"""

    return {
        "method": "POST",
        "url": "={{ $('Loop Monitors').item.json.results_url }}",
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [
                {
                    "name": "X-LeadPilot-Run-Token",
                    "value": "={{ $('Loop Monitors').item.json.progress_token }}",
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
    print(f"  backend: {API_URL}")

    if not WORKFLOW_SECRET:
        print(
            "\n  WARNING: no LEADPILOT_N8N_SECRET baked in. /monitors/due refuses\n"
            "  every request without it, so the workflow will 401. Regenerate with:\n"
            "    LEADPILOT_N8N_SECRET=<secret> python scripts/build_monitor_workflow.py\n"
            "  and set the same value as N8N_WEBHOOK_SECRET in the backend .env."
        )
