# -*- coding: utf-8 -*-

# Copyright IBM Corp. 2025, 2026
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import pytest

from ansible_collections.hashicorp.terraform.plugins.module_utils.exceptions import (
    TerraformError,
)
from ansible_collections.hashicorp.terraform.plugins.module_utils.plan_analyze import (
    BLOCKED_DEFAULT,
    RISKY_DEFAULT,
    SAFE_DEFAULT,
    analyze_plan,
    classify_paths,
    diff_attribute_paths,
    validate_format_version,
)


class TestValidateFormatVersion:
    def test_accepts_1_x(self):
        validate_format_version({"format_version": "1.2"})

    def test_missing_version_raises(self):
        with pytest.raises(TerraformError, match="missing 'format_version'"):
            validate_format_version({})

    def test_2_x_raises(self):
        with pytest.raises(TerraformError, match="Unsupported plan format_version"):
            validate_format_version({"format_version": "2.0"})

    def test_non_numeric_raises(self):
        with pytest.raises(TerraformError, match="Unrecognized plan format_version"):
            validate_format_version({"format_version": "beta"})


class TestDiffAttributePaths:
    def test_scalar_change(self):
        change = {"before": {"instance_type": "t2.micro"}, "after": {"instance_type": "t3.small"}}
        changed, unknown = diff_attribute_paths(change)
        assert changed == ["instance_type"]
        assert unknown == []

    def test_nested_change(self):
        change = {
            "before": {"tags": {"role": "web"}},
            "after": {"tags": {"role": "db"}},
        }
        changed, unknown = diff_attribute_paths(change)
        assert changed == ["tags.role"]

    def test_create_marks_all_after_keys(self):
        change = {"before": None, "after": {"ami": "ami-123", "instance_type": "t2.micro"}}
        changed, _unknown = diff_attribute_paths(change)
        assert set(changed) == {"ami", "instance_type"}

    def test_list_element_change(self):
        change = {
            "before": {"ingress": [{"port": 80}]},
            "after": {"ingress": [{"port": 443}]},
        }
        changed, _unknown = diff_attribute_paths(change)
        assert changed == ["ingress[0].port"]

    def test_list_length_change(self):
        change = {"before": {"ingress": [1]}, "after": {"ingress": [1, 2]}}
        changed, _unknown = diff_attribute_paths(change)
        assert "ingress[1]" in changed

    def test_unknown_tracked_separately(self):
        change = {
            "before": {"private_dns": "old"},
            "after": {"private_dns": None},
            "after_unknown": {"private_dns": True},
        }
        changed, unknown = diff_attribute_paths(change)
        assert unknown == ["private_dns"]
        assert "private_dns" not in changed

    def test_no_change(self):
        change = {"before": {"a": 1}, "after": {"a": 1}}
        changed, unknown = diff_attribute_paths(change)
        assert changed == []
        assert unknown == []


class TestClassifyPaths:
    def test_issue_example(self):
        classification, counts, summary = classify_paths(
            ["tags.role", "instance_type"],
            ["private_dns"],
            SAFE_DEFAULT,
            RISKY_DEFAULT,
            BLOCKED_DEFAULT,
        )
        assert classification == "risky"
        assert counts == {"blocked": 0, "risky": 1, "safe": 1, "unknown": 1}
        assert summary == "1 risky, 1 safe, 1 unknown"

    def test_blocked_precedence(self):
        classification, counts, _summary = classify_paths(
            ["ami", "instance_type", "tags"],
            [],
            SAFE_DEFAULT,
            RISKY_DEFAULT,
            BLOCKED_DEFAULT,
        )
        assert classification == "blocked"
        assert counts["blocked"] == 1

    def test_unmatched_defaults_to_safe(self):
        classification, counts, _summary = classify_paths(
            ["some_random_attr"],
            [],
            SAFE_DEFAULT,
            RISKY_DEFAULT,
            BLOCKED_DEFAULT,
        )
        assert classification == "safe"
        assert counts["safe"] == 1

    def test_only_unknown(self):
        classification, counts, summary = classify_paths(
            [],
            ["private_dns"],
            SAFE_DEFAULT,
            RISKY_DEFAULT,
            BLOCKED_DEFAULT,
        )
        assert classification == "unknown"
        assert summary == "1 unknown"

    def test_list_index_path_matches_attribute(self):
        classification, _counts, _summary = classify_paths(
            ["ingress[0].port"],
            [],
            SAFE_DEFAULT,
            RISKY_DEFAULT,
            BLOCKED_DEFAULT,
        )
        assert classification == "risky"


def _plan(**kwargs):
    base = {"format_version": "1.2"}
    base.update(kwargs)
    return base


class TestAnalyzePlan:
    def test_unsupported_version_raises(self):
        with pytest.raises(TerraformError):
            analyze_plan({"format_version": "2.0"})

    def test_empty_plan(self):
        result = analyze_plan(_plan())
        assert result["has_drift"] is False
        assert result["drift_count"] == 0
        assert result["has_changes"] is False
        assert result["change_count"] == 0
        assert result["resource_changes"] == []
        assert result["summary"] == {"safe": 0, "risky": 0, "blocked": 0, "unknown": 0}

    def test_skips_noop(self):
        result = analyze_plan(
            _plan(
                resource_changes=[
                    {"address": "aws_instance.web", "change": {"actions": ["no-op"]}},
                ],
            ),
        )
        assert result["change_count"] == 0
        assert result["resource_changes"] == []

    def test_resource_change_classified(self):
        result = analyze_plan(
            _plan(
                resource_changes=[
                    {
                        "address": "aws_instance.web",
                        "type": "aws_instance",
                        "name": "web",
                        "provider_name": "registry.terraform.io/hashicorp/aws",
                        "mode": "managed",
                        "change": {
                            "actions": ["update"],
                            "before": {"instance_type": "t2.micro", "tags": {"role": "web"}},
                            "after": {"instance_type": "t3.small", "tags": {"role": "db"}},
                        },
                    },
                ],
            ),
        )
        assert result["has_changes"] is True
        assert result["change_count"] == 1
        entry = result["resource_changes"][0]
        assert entry["source"] == "resource_changes"
        assert entry["classification"] == "risky"
        assert set(entry["changed_attributes"]) == {"instance_type", "tags.role"}
        assert result["summary"]["risky"] == 1

    def test_drift_detected(self):
        result = analyze_plan(
            _plan(
                resource_drift=[
                    {
                        "address": "aws_instance.web",
                        "change": {
                            "actions": ["update"],
                            "before": {"ami": "ami-old"},
                            "after": {"ami": "ami-new"},
                        },
                    },
                ],
            ),
        )
        assert result["has_drift"] is True
        assert result["drift_count"] == 1
        entry = result["resource_changes"][0]
        assert entry["source"] == "resource_drift"
        assert entry["classification"] == "blocked"

    def test_detect_drift_disabled(self):
        result = analyze_plan(
            _plan(
                resource_drift=[
                    {"address": "aws_instance.web", "change": {"actions": ["update"], "before": {"ami": "a"}, "after": {"ami": "b"}}},
                ],
            ),
            detect_drift=False,
        )
        assert result["drift_count"] == 0

    def test_include_resource_changes_disabled(self):
        result = analyze_plan(
            _plan(
                resource_changes=[
                    {"address": "aws_instance.web", "change": {"actions": ["update"], "before": {"ami": "a"}, "after": {"ami": "b"}}},
                ],
            ),
            include_resource_changes=False,
        )
        assert result["change_count"] == 0

    def test_output_changes(self):
        result = analyze_plan(
            _plan(
                output_changes={
                    "endpoint": {"actions": ["update"], "before": "a", "after": "b"},
                    "unchanged": {"actions": ["no-op"]},
                },
            ),
        )
        names = [o["name"] for o in result["output_changes"]]
        assert names == ["endpoint"]

    def test_include_output_changes_disabled(self):
        result = analyze_plan(
            _plan(output_changes={"endpoint": {"actions": ["update"], "before": "a", "after": "b"}}),
            include_output_changes=False,
        )
        assert result["output_changes"] == []

    def test_include_values_masks_sensitive(self):
        result = analyze_plan(
            _plan(
                resource_changes=[
                    {
                        "address": "aws_db_instance.main",
                        "change": {
                            "actions": ["update"],
                            "before": {"password": "old", "instance_type": "db.t3.micro"},
                            "after": {"password": "new", "instance_type": "db.t3.small"},
                            "before_sensitive": {"password": True},
                            "after_sensitive": {"password": True},
                        },
                    },
                ],
            ),
            include_values=True,
        )
        entry = result["resource_changes"][0]
        assert entry["before"]["password"] == ""
        assert entry["after"]["password"] == ""
        assert entry["after"]["instance_type"] == "db.t3.small"

    def test_include_values_default_omits_values(self):
        result = analyze_plan(
            _plan(
                resource_changes=[
                    {"address": "aws_instance.web", "change": {"actions": ["update"], "before": {"ami": "a"}, "after": {"ami": "b"}}},
                ],
            ),
        )
        assert "before" not in result["resource_changes"][0]

    def test_custom_classification_lists(self):
        result = analyze_plan(
            _plan(
                resource_changes=[
                    {"address": "x.y", "change": {"actions": ["update"], "before": {"custom_attr": 1}, "after": {"custom_attr": 2}}},
                ],
            ),
            blocked_attributes=["custom_attr"],
        )
        assert result["resource_changes"][0]["classification"] == "blocked"

    def test_drift_and_changes_both_counted(self):
        result = analyze_plan(
            _plan(
                resource_drift=[
                    {"address": "aws_instance.web", "change": {"actions": ["update"], "before": {"ami": "a"}, "after": {"ami": "b"}}},
                ],
                resource_changes=[
                    {"address": "aws_instance.web", "change": {"actions": ["update"], "before": {"tags": {"x": 1}}, "after": {"tags": {"x": 2}}}},
                ],
            ),
        )
        assert result["drift_count"] == 1
        assert result["change_count"] == 1
        assert len(result["resource_changes"]) == 2
        sources = {e["source"] for e in result["resource_changes"]}
        assert sources == {"resource_drift", "resource_changes"}
