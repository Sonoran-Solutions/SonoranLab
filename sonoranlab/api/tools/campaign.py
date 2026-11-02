"""Class campaign ledger tools (DYL-129). Owner decisions (budget grants, resume, delivery) are CLI only."""

from sonoranlab import campaign
from sonoranlab.api.registry import Tool

_ID = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$", "maxLength": 128}
_CAMPAIGN = {"type": "string", "pattern": f"^{campaign.CAMPAIGN_ID}$"}
_TEXT = {"type": "string", "minLength": 1, "maxLength": 4096}


def _schema(properties: dict, required: list[str], **extra) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False, **extra}


CREATE_SCHEMA = _schema(
    {
        "project": _ID,
        "request": {"type": "object", "maxProperties": 32},
        "reference_id": {"type": "string", "pattern": "^refs-[0-9a-f]{12}$"},
        "revise": _CAMPAIGN,
        "max_seeds_per_clip": {"type": "integer", "minimum": 1, "maximum": 1000},
        "budget_decision_ref": {"type": "string", "minLength": 1, "maxLength": 256},
    },
    ["project", "request", "reference_id"],
)
STATUS_SCHEMA = _schema({"campaign_id": _CAMPAIGN}, ["campaign_id"])
SUBMIT_SCHEMA = _schema(
    {"campaign_id": _CAMPAIGN, "clip": _ID, "seed": {"type": "integer", "minimum": 0, "maximum": 2**31 - 1}},
    ["campaign_id", "clip"],
)
DECIDE_SCHEMA = _schema(
    {
        "campaign_id": _CAMPAIGN,
        "job_id": {"type": "string", "pattern": "^job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$"},
        "decision": {"enum": ["selected", "rejected"]},
        "reason": _TEXT,
    },
    ["campaign_id", "job_id", "decision", "reason"],
)
ESCALATE_SCHEMA = _schema({"campaign_id": _CAMPAIGN, "clip": _ID, "reason": _TEXT}, ["campaign_id", "clip", "reason"])
NOTE_SCHEMA = _schema(
    {
        "campaign_id": _CAMPAIGN,
        "text": _TEXT,
        "cost": _schema(
            {
                "source": {"type": "string", "minLength": 1, "maxLength": 200},
                "units": {"type": "string", "minLength": 1, "maxLength": 64},
                "amount": {"type": "number", "minimum": 0},
            },
            ["source", "units", "amount"],
        ),
    },
    ["campaign_id"],
    anyOf=[{"required": ["text"]}, {"required": ["cost"]}],
)

TOOLS = (
    Tool(
        "campaign_create",
        "Admit a class request + approved reference set and freeze a campaign (or revision N+1 via revise).",
        CREATE_SCHEMA,
        False,
        "sync",
        lambda p, c: campaign.create(**p, ctx=c),
    ),
    Tool(
        "campaign_status",
        "Derived campaign/clip states, budgets, candidates and next allowed actions; reconciles jobs read-only.",
        STATUS_SCHEMA,
        True,
        "sync",
        lambda p, c: campaign.status(**p, ctx=c),
    ),
    Tool(
        "campaign_submit",
        "The campaign's only generation path: one Track E animate job with the next unused seed, within budget.",
        SUBMIT_SCHEMA,
        False,
        "job",
        lambda p, c: campaign.submit(**p, ctx=c),
    ),
    Tool(
        "campaign_decide",
        "Record the agent's candidate recommendation (selected needs a technical PASS); never an approval.",
        DECIDE_SCHEMA,
        False,
        "sync",
        lambda p, c: campaign.decide(**p, ctx=c),
    ),
    Tool(
        "campaign_escalate",
        "Record an escalation to the owner with the best two candidates and their diagnostics.",
        ESCALATE_SCHEMA,
        False,
        "sync",
        lambda p, c: campaign.escalate(**p, ctx=c),
    ),
    Tool(
        "campaign_report",
        "Per-clip job ids, seeds, failure classes, timings, explicit costs and interventions (nulls never zero).",
        STATUS_SCHEMA,
        True,
        "sync",
        lambda p, c: campaign.report(**p, ctx=c),
    ),
    Tool(
        "campaign_note",
        "Append an agent note and/or an explicit cost record (source, units, amount) to the campaign ledger.",
        NOTE_SCHEMA,
        False,
        "sync",
        lambda p, c: campaign.note(**p, ctx=c),
    ),
)
