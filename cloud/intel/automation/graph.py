"""Workflow graphs: branching, delays and approval steps on top of the flat engine.

A workflow whose ``graph`` has ``nodes`` runs as a graph; otherwise it runs its
flat ``actions`` list exactly as before. A graph is::

    {"start": "n1",
     "nodes": {
       "n1": {"type": "condition", "conditions": {...}, "then": "n2", "else": "n4"},
       "n2": {"type": "action", "action": {"type": "create_task", ...}, "next": "n3",
              "retry": {"max_attempts": 3, "backoff_seconds": 60}},
       "n3": {"type": "delay", "days": 2, "next": "n4"},
       "n4": {"type": "approval", "message": "OK to enroll?", "next": "n5", "on_reject": null},
       "n5": {"type": "end"}},
     "schedule": {"every_minutes": 1440},     # only for the "schedule" trigger
     "ui": [...]}                             # the builder's own step tree; ignored here

A node whose ``next``/``then``/``else`` is missing or null ends the run. Graphs
must be acyclic (checked at save time), so a run always terminates.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from cloud.intel.core.context import ValidationError

__all__ = ["NODE_TYPES", "MAX_DELAY_SECONDS", "delay_seconds", "is_graph", "retry_policy", "validate_graph"]

NODE_TYPES = ("condition", "action", "delay", "approval", "end")
MAX_DELAY_SECONDS = 90 * 86400
MAX_NODES = 100
_UNITS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}
_EDGES = ("next", "then", "else", "on_reject")


def is_graph(graph: Any) -> bool:
    return isinstance(graph, Mapping) and isinstance(graph.get("nodes"), Mapping) and bool(graph.get("nodes"))


def delay_seconds(node: Mapping[str, Any]) -> int:
    total = 0.0
    for unit, factor in _UNITS.items():
        value = node.get(unit)
        if value in (None, ""):
            continue
        try:
            total += float(value) * factor
        except (TypeError, ValueError):
            raise ValidationError(f"delay {unit} must be a number") from None
    return int(total)


def retry_policy(workflow: Mapping[str, Any], node: Optional[Mapping[str, Any]] = None) -> Dict[str, int]:
    """max_attempts and backoff_seconds for an action node: node > workflow > defaults."""
    retry_default = 3 if workflow.get("failure_policy") == "retry" else 1
    policy = {"max_attempts": retry_default, "backoff_seconds": 60}
    for source in (workflow.get("retry_policy") or {}, (node or {}).get("retry") or {}):
        for key in policy:
            if source.get(key) not in (None, ""):
                try:
                    policy[key] = int(source[key])
                except (TypeError, ValueError):
                    raise ValidationError(f"retry {key} must be a whole number") from None
    policy["max_attempts"] = max(1, min(10, policy["max_attempts"]))
    policy["backoff_seconds"] = max(0, min(86400, policy["backoff_seconds"]))
    return policy


def validate_graph(graph: Mapping[str, Any], *, validate_conditions: Callable[[Any], None],
                   normalise_action: Callable[[Mapping[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """Check a graph and return it with its actions normalised. Raises ValidationError."""
    if not is_graph(graph):
        raise ValidationError("a workflow graph needs nodes")
    nodes = graph["nodes"]
    if len(nodes) > MAX_NODES:
        raise ValidationError(f"a workflow graph can have at most {MAX_NODES} steps")
    start = graph.get("start")
    if start not in nodes:
        raise ValidationError("the graph's start must name one of its nodes")
    out_nodes: Dict[str, Dict[str, Any]] = {}
    for node_id, node in nodes.items():
        if not isinstance(node, Mapping):
            raise ValidationError(f"step {node_id} must be an object")
        if len(str(node_id)) > 80:
            raise ValidationError("step ids are at most 80 characters")
        kind = node.get("type")
        if kind not in NODE_TYPES:
            raise ValidationError(f"step {node_id}: type must be one of {', '.join(NODE_TYPES)}")
        node = dict(node)
        for edge in _EDGES:
            target = node.get(edge)
            if target not in (None, "") and target not in nodes:
                raise ValidationError(f"step {node_id}: {edge} points to an unknown step {target!r}")
        if kind == "condition":
            validate_conditions(node.get("conditions"))
        elif kind == "action":
            if not isinstance(node.get("action"), Mapping):
                raise ValidationError(f"step {node_id}: an action step needs an action")
            node["action"] = normalise_action(node["action"])
            if node.get("retry") is not None and not isinstance(node["retry"], Mapping):
                raise ValidationError(f"step {node_id}: retry must be an object")
        elif kind == "delay":
            seconds = delay_seconds(node)
            if seconds <= 0 or seconds > MAX_DELAY_SECONDS:
                raise ValidationError(f"step {node_id}: a delay must be between 1 second and 90 days")
        out_nodes[str(node_id)] = node
    _check_acyclic(out_nodes, start)
    out = dict(graph)
    out["nodes"] = out_nodes
    schedule = graph.get("schedule")
    if schedule is not None:
        _check_schedule(schedule)
    return out


def _check_schedule(schedule: Any) -> None:
    if not isinstance(schedule, Mapping):
        raise ValidationError("schedule must be an object")
    try:
        every = int(schedule.get("every_minutes") or 0)
    except (TypeError, ValueError):
        raise ValidationError("schedule.every_minutes must be a whole number") from None
    if every < 5 or every > 60 * 24 * 31:
        raise ValidationError("schedule.every_minutes must be between 5 minutes and 31 days")


def _check_acyclic(nodes: Mapping[str, Mapping[str, Any]], start: str) -> None:
    state: Dict[str, int] = {}  # 1 = on the current path, 2 = done

    def visit(node_id: str) -> None:
        if state.get(node_id) == 2:
            return
        if state.get(node_id) == 1:
            raise ValidationError(f"the workflow loops back to step {node_id}; loops are not allowed")
        state[node_id] = 1
        for edge in _EDGES:
            target = nodes[node_id].get(edge)
            if target:
                visit(target)
        state[node_id] = 2

    for node_id in [start, *nodes]:
        visit(node_id)
