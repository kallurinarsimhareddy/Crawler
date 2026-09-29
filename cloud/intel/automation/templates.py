"""Ready-made workflows. Creating one from a template saves it **disabled**; the
user fills in ids (lists, sequences, campaigns) and enables it explicitly."""

from __future__ import annotations

import copy
from typing import Any, Dict, List

__all__ = ["TEMPLATES", "template"]

TEMPLATES: List[Dict[str, Any]] = [
    {
        "key": "hiring_spike_task_notify",
        "name": "New hiring spike → create task + notify",
        "description": "When a company shows a hiring spike, open a research task and tell the team.",
        "trigger": "hiring_spike",
        "graph": {"start": "task", "nodes": {
            "task": {"type": "action", "next": "notify",
                     "action": {"type": "create_task", "title": "Research hiring spike at {company.name}",
                                "due_in_days": 2, "priority": "high"}},
            "notify": {"type": "action",
                       "action": {"type": "send_notification", "title": "Hiring spike: {company.name}",
                                  "body": "A research task was created.", "severity": "info"}},
        }},
    },
    {
        "key": "reply_stop_task",
        "name": "Reply received → stop sequence + create task",
        "description": "Sequences already stop on reply; this adds a follow-up task and a notification.",
        "trigger": "reply_received",
        "graph": {"start": "task", "nodes": {
            "task": {"type": "action", "next": "notify",
                     "action": {"type": "create_task", "title": "Reply from {contact.full_name} — follow up",
                                "due_in_days": 0, "priority": "urgent"}},
            "notify": {"type": "action",
                       "action": {"type": "send_notification", "title": "{contact.full_name} replied",
                                  "severity": "success"}},
        }},
    },
    {
        "key": "validated_list_propose_campaign",
        "name": "Validated list → propose campaign",
        "description": "After a validation job finishes, wait for a manager's approval, then add the "
                       "company to a campaign and create a review task. Nothing is sent.",
        "trigger": "email_validated",
        "graph": {"start": "valid", "nodes": {
            "valid": {"type": "condition", "then": "approve", "else": None,
                      "conditions": {"field": "payload.status", "op": "eq", "value": "VALID"}},
            "approve": {"type": "approval", "message": "Add this contact's company to the campaign?",
                        "next": "campaign", "on_reject": None},
            "campaign": {"type": "action", "next": "task",
                         "action": {"type": "add_to_campaign", "campaign_id": ""}},
            "task": {"type": "action",
                     "action": {"type": "create_task", "title": "Review campaign audience", "due_in_days": 1}},
        }},
    },
    {
        "key": "new_it_leader_validate_wait",
        "name": "New IT leader → validate email, wait 2 days, follow up",
        "description": "Validate the address (free checks only), wait two days, then create a follow-up task.",
        "trigger": "new_contact",
        "conditions": {"any": [{"field": "contact.title", "op": "contains", "value": "CIO"},
                               {"field": "contact.title", "op": "contains", "value": "CTO"},
                               {"field": "contact.function", "op": "eq", "value": "it"}]},
        "graph": {"start": "validate", "nodes": {
            "validate": {"type": "action", "next": "wait",
                         "action": {"type": "validate_email", "allow_paid": False}},
            "wait": {"type": "delay", "days": 2, "next": "task"},
            "task": {"type": "action",
                     "action": {"type": "create_task", "title": "Follow up with {contact.full_name}",
                                "priority": "normal"}},
        }},
    },
    {
        "key": "scrape_completed_research",
        "name": "Scrape completed → notify + propose research",
        "description": "When an AI scrape finishes, notify the owner and plan a research run for review.",
        "trigger": "scrape_completed",
        "graph": {"start": "notify", "nodes": {
            "notify": {"type": "action", "next": "research",
                       "action": {"type": "send_notification", "title": "Scrape finished",
                                  "body": "Review the results and proposals.", "severity": "info"}},
            "research": {"type": "action",
                         "action": {"type": "start_research",
                                    "question": "Research the companies found by the latest scrape"}},
        }},
    },
    {
        "key": "weekly_digest",
        "name": "Weekly → export companies + notify",
        "description": "Every week, export the company list and post a notification.",
        "trigger": "schedule",
        "graph": {"start": "export", "schedule": {"every_minutes": 10080}, "nodes": {
            "export": {"type": "action", "next": "notify",
                       "action": {"type": "export", "entity_type": "companies", "format": "csv"}},
            "notify": {"type": "action",
                       "action": {"type": "send_notification", "title": "Weekly company export is ready"}},
        }},
    },
]


def template(key: str) -> Dict[str, Any]:
    for item in TEMPLATES:
        if item["key"] == key:
            return copy.deepcopy(item)
    raise KeyError(key)
