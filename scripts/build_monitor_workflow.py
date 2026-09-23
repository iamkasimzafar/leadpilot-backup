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
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "n8n_workflow" / "LeadPilot.json"
TARGET = ROOT / "n8n_workflow" / "LeadPilot - Radar Monitors.json"

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
                "url": "={{ $env.LEADPILOT_API_URL }}/api/v1/lead-radar/monitors/due",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {
                            "name": "X-LeadPilot-Token",
                            "value": "={{ $env.LEADPILOT_N8N_SECRET }}",
                        }
                    ]
                },
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

        if clone["name"] == "serper":
            clone["parameters"] = _serper_params()
            clone["notes"] = (
                "`page` comes from the monitor's stored serper_offset. Without "
                "it every run re-reads the same top results and finds nothing "
                "new from day two on."
            )

        if clone["name"] == "Send Results to Backend":
            clone["parameters"] = _results_params()
            clone["name"] = "Post Monitor Results"
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

    return {
        "name": "LeadPilot - Radar Monitors",
        "nodes": nodes,
        "pinData": {},
        "connections": connections,
        "active": False,
        "settings": source.get("settings", {}),
        "meta": source.get("meta", {}),
        "tags": [],
    }


def _serper_params() -> dict:
    """Serper, asking for the monitor's page rather than always the first."""
    js = """={{
JSON.stringify((() => {
  const m = $('Loop Monitors').item.json;

  // Company-type OR-group, same idea as the manual search. A monitor has no
  // company_type of its own, so the plain term is used.
  const q = m.search_term;

  return {
    q,
    gl: m.country || undefined,
    num: Math.min(m.limit_per_run || 50, 100),
    // The whole point of the monitor: start where the last run stopped.
    page: m.serper_page || 1
  };
})())
}}"""

    return {
        "method": "POST",
        "url": "https://google.serper.dev/search",
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [
                {"name": "X-API-KEY", "value": "={{ $env.SERPER_API_KEY }}"},
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
    body = """={
  "monitor_id": "{{ $('Loop Monitors').item.json.monitor_id }}",
  "status": "completed",
  "companies": {{ JSON.stringify($input.all().map(i => i.json)) }}
}"""

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
