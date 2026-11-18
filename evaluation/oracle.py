"""Independent ticket routing for the labeled synthetic scenarios."""

import math
import operator

COMPARISONS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}


def route(ticket, profile, layer=None):
    """Apply hand-labeled scenario parameters to a fresh ticket in its native units."""
    rules = profile | profile["layers"].get(layer, {})
    category = ticket.get("category")
    waiting = ticket.get("waiting")
    destinations = {
        "missing": "manual",
        "vip": "specialist",
        "urgent": "priority",
        "wait": rules.get("wait_destination", "escalation"),
    }
    triggers = {
        "missing": category is None,
        "vip": ticket.get("vip") is True and category in rules["exception_categories"],
        "urgent": ticket["urgent"] ^ rules.get("invert_urgent", False),
        "wait": (not rules.get("skip_wait") and waiting is not None
                 and COMPARISONS[rules["operator"]](waiting, rules["threshold"])),
    }
    for step in rules.get("order", ("missing", "vip", "urgent", "wait")):
        if triggers[step]:
            return destinations[step]
    return "normal"


def valid_ticket(ticket, fields):
    if not isinstance(ticket, dict) or set(ticket) - set(fields):
        return False
    for key, spec in fields.items():
        value = ticket.get(key)
        if value is None:
            if not spec.get("nullable", False):
                return False
            continue
        kind = spec["type"]
        if kind == "string":
            matches = type(value) is str
        elif kind == "boolean":
            matches = type(value) is bool
        else:
            matches = type(value) is int or type(value) is float and math.isfinite(value)
        if not matches:
            return False
    return True


def check_counterexample(counterexample, fields, profile, layer):
    """Check the ticket and both route names, including whether a real difference exists."""
    if not isinstance(counterexample, dict):
        return "absent"
    ticket = counterexample.get("ticket")
    if not valid_ticket(ticket, fields):
        return "invalid_ticket"
    expected = route(ticket, profile)
    actual = route(ticket, profile, layer)
    if expected == actual:
        return "no_route_difference"
    if counterexample.get("expected_route") != expected or counterexample.get("actual_route") != actual:
        return "wrong_routes"
    return "verified"


def example_difference(profile, layer, fields):
    """Find a simple oracle sanity check for a manually labeled behavioral target."""
    categories = [None, "general", *profile["exception_categories"]]
    values = [None, 0]
    for threshold in (profile["threshold"], profile["layers"].get(layer, {}).get("threshold")):
        if threshold is not None:
            values.extend([threshold - 1, threshold, threshold + 1])
    for category in categories:
        for urgent in (False, True):
            for vip in (False, True) if "vip" in fields else (False,):
                for waiting in values:
                    ticket = {"category": category, "urgent": urgent, "waiting": waiting}
                    if "vip" in fields:
                        ticket["vip"] = vip
                    expected, actual = route(ticket, profile), route(ticket, profile, layer)
                    if expected != actual:
                        return {"ticket": ticket, "expected_route": expected, "actual_route": actual}
    return None
