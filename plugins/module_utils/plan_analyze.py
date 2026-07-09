# -*- coding: utf-8 -*-

# Copyright IBM Corp. 2025, 2026
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""Pure analysis helpers for the ``plan_analyze`` module.

This module contains no API/transport logic. It takes a Terraform plan JSON
document (the ``json-output`` shape produced by Terraform 1.x) and derives
Ansible-friendly drift and change facts: which resources are affected, which
attribute paths changed, and a ``safe``/``risky``/``blocked`` classification per
resource. Keeping it side-effect free makes it unit-testable without pytfe or a
live organization.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from ansible_collections.hashicorp.terraform.plugins.module_utils.exceptions import (
    TerraformError,
)

# Default attribute classification lists. These are intentionally
# provider-agnostic starting points; consumers override them via the module's
# safe_attributes / risky_attributes / blocked_attributes options.
SAFE_DEFAULT = ["tags", "public_ip", "private_ip", "metadata", "computed_outputs"]
RISKY_DEFAULT = ["ingress", "egress", "security_group_rules", "instance_type", "desired_capacity"]
BLOCKED_DEFAULT = ["iam_policy", "subnet_id", "vpc_id", "ami", "kms_key_id"]

# Value substituted for attributes flagged sensitive when include_values=True.
SENSITIVE_MASK = ""

# A record whose only action is "no-op" represents no change and is skipped.
_NOOP_ACTIONS = frozenset({"no-op"})

# Classification severity, highest first. Used both for precedence and for
# rendering the human-readable change_summary.
_SEVERITY_ORDER = ["blocked", "risky", "safe", "unknown"]


def validate_format_version(plan_json: Dict[str, Any]) -> None:
    """Ensure the plan JSON uses a schema this analyzer understands.

    Terraform's plan ``json-output`` carries a ``format_version`` string. This
    analyzer targets the 1.x family. A 2.x+ document may reshape fields in ways
    that would silently mis-parse, so fail loudly instead.

    Args:
        plan_json: The parsed Terraform plan JSON document.

    Raises:
        TerraformError: If ``format_version`` is missing or its major version is
            2 or greater.
    """
    version = plan_json.get("format_version")
    if not version:
        raise TerraformError("Plan JSON is missing 'format_version'; cannot determine schema compatibility.")

    major = str(version).split(".", 1)[0]
    try:
        major_int = int(major)
    except ValueError as exc:
        raise TerraformError(f"Unrecognized plan format_version '{version}'.") from exc

    if major_int >= 2:
        raise TerraformError(
            f"Unsupported plan format_version '{version}'. plan_analyze supports Terraform 1.x plan JSON.",
        )


def _clean_segments(path: str) -> Set[str]:
    """Return the attribute-name segments of a diff path, stripped of indices.

    ``ingress[0].cidr_blocks[1]`` -> {"ingress", "cidr_blocks"}. Used for
    matching a changed path against the classification lists.
    """
    segments = set()
    for raw in path.split("."):
        base = raw.split("[", 1)[0]
        if base:
            segments.add(base)
    return segments


def _diff_paths(before: Any, after: Any, prefix: str, changed: Set[str]) -> None:
    """Recursively collect leaf paths where ``before`` and ``after`` differ.

    A missing side (``None`` on create/delete) is treated as an empty container
    when the other side is a dict/list, so every added or removed leaf is
    reported rather than collapsing to the container root.
    """
    if isinstance(before, dict) or isinstance(after, dict):
        b_map = before if isinstance(before, dict) else {}
        a_map = after if isinstance(after, dict) else {}
        for key in set(b_map) | set(a_map):
            child = f"{prefix}.{key}" if prefix else key
            _diff_paths(b_map.get(key), a_map.get(key), child, changed)
    elif isinstance(before, list) or isinstance(after, list):
        b_list = before if isinstance(before, list) else []
        a_list = after if isinstance(after, list) else []
        for index in range(max(len(b_list), len(a_list))):
            b_item = b_list[index] if index < len(b_list) else None
            a_item = a_list[index] if index < len(a_list) else None
            _diff_paths(b_item, a_item, f"{prefix}[{index}]", changed)
    elif before != after:
        # Scalars, or a type change (dict<->scalar, list<->null, etc.).
        changed.add(prefix)


def _unknown_paths(after_unknown: Any, prefix: str, unknown: Set[str]) -> None:
    """Collect paths marked computed (unknown-after-apply) in ``after_unknown``."""
    if after_unknown is True:
        unknown.add(prefix)
    elif isinstance(after_unknown, dict):
        for key, value in after_unknown.items():
            child = f"{prefix}.{key}" if prefix else key
            _unknown_paths(value, child, unknown)
    elif isinstance(after_unknown, list):
        for index, value in enumerate(after_unknown):
            _unknown_paths(value, f"{prefix}[{index}]", unknown)


def diff_attribute_paths(change: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Compute the changed and unknown attribute paths for a single change block.

    Args:
        change: The ``change`` object of a resource_changes / resource_drift
            record, containing ``before``, ``after``, and ``after_unknown``.

    Returns:
        A tuple ``(changed_attributes, unknown_attributes)`` of sorted, unique
        dotted paths. Paths that are computed (unknown-after-apply) appear only
        in ``unknown_attributes``, never in ``changed_attributes``.
    """
    changed: Set[str] = set()
    unknown: Set[str] = set()

    _diff_paths(change.get("before"), change.get("after"), "", changed)
    _unknown_paths(change.get("after_unknown"), "", unknown)

    # Computed values are reported separately; keep the two sets disjoint.
    changed -= unknown
    changed.discard("")
    unknown.discard("")

    return sorted(changed), sorted(unknown)


def classify_paths(
    changed_attributes: List[str],
    unknown_attributes: List[str],
    safe: List[str],
    risky: List[str],
    blocked: List[str],
) -> Tuple[str, Dict[str, int], str]:
    """Classify a resource's attribute changes.

    Each changed path is bucketed by attribute name with precedence
    ``blocked > risky > safe``; a changed path matching none of the lists
    defaults to ``safe``. Computed (unknown) paths count as ``unknown``.

    Args:
        changed_attributes: Concrete changed attribute paths.
        unknown_attributes: Computed (unknown-after-apply) attribute paths.
        safe: Attribute names considered safe.
        risky: Attribute names considered risky.
        blocked: Attribute names considered blocked.

    Returns:
        A tuple ``(classification, counts, change_summary)`` where
        ``classification`` is the resource-level verdict (precedence
        ``blocked > risky > safe > unknown``), ``counts`` maps each category to
        the number of attributes in it, and ``change_summary`` is a
        human-readable string like ``"1 risky, 1 safe, 1 unknown"``.
    """
    counts = {"blocked": 0, "risky": 0, "safe": 0, "unknown": len(unknown_attributes)}

    for path in changed_attributes:
        segments = _clean_segments(path)
        if segments & set(blocked):
            counts["blocked"] += 1
        elif segments & set(risky):
            counts["risky"] += 1
        else:
            # Explicitly-safe attributes and anything unmatched are benign.
            counts["safe"] += 1

    classification = "safe"
    for category in _SEVERITY_ORDER:
        if counts[category]:
            classification = category
            break

    change_summary = ", ".join(f"{counts[c]} {c}" for c in _SEVERITY_ORDER if counts[c])

    return classification, counts, change_summary


def _mask_sensitive(value: Any, sensitive: Any) -> Any:
    """Recursively replace sensitive leaves in ``value`` with SENSITIVE_MASK."""
    if sensitive is True:
        return SENSITIVE_MASK
    if isinstance(value, dict) and isinstance(sensitive, dict):
        return {k: _mask_sensitive(v, sensitive.get(k, False)) for k, v in value.items()}
    if isinstance(value, list) and isinstance(sensitive, list):
        return [_mask_sensitive(item, sensitive[i] if i < len(sensitive) else False) for i, item in enumerate(value)]
    return value


def _analyze_record(
    record: Dict[str, Any],
    source: str,
    safe: List[str],
    risky: List[str],
    blocked: List[str],
    include_values: bool,
) -> Dict[str, Any] | None:
    """Build a per-resource analysis entry, or ``None`` if it is a no-op."""
    change = record.get("change") or {}
    actions = change.get("actions") or []

    if set(actions) <= _NOOP_ACTIONS:
        return None

    changed_attributes, unknown_attributes = diff_attribute_paths(change)
    classification, _counts, change_summary = classify_paths(
        changed_attributes,
        unknown_attributes,
        safe,
        risky,
        blocked,
    )

    entry = {
        "address": record.get("address"),
        "type": record.get("type"),
        "name": record.get("name"),
        "provider_name": record.get("provider_name"),
        "module_address": record.get("module_address"),
        "mode": record.get("mode"),
        "actions": actions,
        "action_reason": record.get("action_reason"),
        "source": source,
        "changed_attributes": changed_attributes,
        "unknown_attributes": unknown_attributes,
        "classification": classification,
        "change_summary": change_summary,
    }

    if include_values:
        entry["before"] = _mask_sensitive(change.get("before"), change.get("before_sensitive"))
        entry["after"] = _mask_sensitive(change.get("after"), change.get("after_sensitive"))

    return entry


def _analyze_output(name: str, change: Dict[str, Any], include_values: bool) -> Dict[str, Any] | None:
    """Build a per-output analysis entry, or ``None`` if it is a no-op."""
    actions = change.get("actions") or []
    if set(actions) <= _NOOP_ACTIONS:
        return None

    entry: Dict[str, Any] = {
        "name": name,
        "actions": actions,
        "sensitive": bool(change.get("after_sensitive") or change.get("before_sensitive")),
    }

    if include_values:
        entry["before"] = _mask_sensitive(change.get("before"), change.get("before_sensitive"))
        entry["after"] = _mask_sensitive(change.get("after"), change.get("after_sensitive"))

    return entry


def analyze_plan(
    plan_json: Dict[str, Any],
    detect_drift: bool = True,
    include_resource_changes: bool = True,
    include_output_changes: bool = True,
    include_values: bool = False,
    safe_attributes: List[str] | None = None,
    risky_attributes: List[str] | None = None,
    blocked_attributes: List[str] | None = None,
) -> Dict[str, Any]:
    """Analyze a Terraform plan JSON document into drift/change facts.

    Args:
        plan_json: The parsed Terraform plan ``json-output`` document (1.x).
        detect_drift: Walk ``resource_drift[]`` for out-of-band drift.
        include_resource_changes: Walk ``resource_changes[]`` for planned changes.
        include_output_changes: Analyze ``output_changes``.
        include_values: Include masked ``before``/``after`` values per entry.
        safe_attributes: Attribute names considered safe (defaults SAFE_DEFAULT).
        risky_attributes: Attribute names considered risky (defaults RISKY_DEFAULT).
        blocked_attributes: Attribute names considered blocked (defaults BLOCKED_DEFAULT).

    Returns:
        A dict with ``has_drift``, ``drift_count``, ``has_changes``,
        ``change_count``, ``resource_changes`` (per-resource entries),
        ``output_changes``, and a ``summary`` of classification counts.

    Raises:
        TerraformError: If the plan JSON ``format_version`` is unsupported.
    """
    validate_format_version(plan_json)

    safe = safe_attributes if safe_attributes is not None else SAFE_DEFAULT
    risky = risky_attributes if risky_attributes is not None else RISKY_DEFAULT
    blocked = blocked_attributes if blocked_attributes is not None else BLOCKED_DEFAULT

    entries: List[Dict[str, Any]] = []
    drift_count = 0
    change_count = 0

    if detect_drift:
        for record in plan_json.get("resource_drift") or []:
            entry = _analyze_record(record, "resource_drift", safe, risky, blocked, include_values)
            if entry is not None:
                entries.append(entry)
                drift_count += 1

    if include_resource_changes:
        for record in plan_json.get("resource_changes") or []:
            entry = _analyze_record(record, "resource_changes", safe, risky, blocked, include_values)
            if entry is not None:
                entries.append(entry)
                change_count += 1

    output_entries: List[Dict[str, Any]] = []
    if include_output_changes:
        for name, change in (plan_json.get("output_changes") or {}).items():
            entry = _analyze_output(name, change, include_values)
            if entry is not None:
                output_entries.append(entry)

    summary = {"safe": 0, "risky": 0, "blocked": 0, "unknown": 0}
    for entry in entries:
        summary[entry["classification"]] += 1

    return {
        "has_drift": drift_count > 0,
        "drift_count": drift_count,
        "has_changes": change_count > 0,
        "change_count": change_count,
        "resource_changes": entries,
        "output_changes": output_entries,
        "summary": summary,
    }
