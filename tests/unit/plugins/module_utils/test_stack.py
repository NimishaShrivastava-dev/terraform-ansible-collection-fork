# -*- coding: utf-8 -*-

# Copyright IBM Corp. 2025, 2026
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""Unit tests for plugins/module_utils/stack.py (pytfe adapter)."""

from unittest.mock import Mock, patch

from pytfe.errors import NotFound

from ansible_collections.hashicorp.terraform.plugins.module_utils.stack import (
    create_stack,
    delete_stack,
    get_stack,
    get_stack_by_name,
    update_stack,
)

MU_PATH = "ansible_collections.hashicorp.terraform.plugins.module_utils.stack"


def _make_model(payload):
    m = Mock()
    m.model_dump.return_value = payload
    return m


class TestGetStack:
    def test_success(self):
        adapter = Mock()
        adapter.client.stacks.read.return_value = _make_model({"id": "st-1", "name": "stack-a"})
        assert get_stack(adapter, "st-1") == {"id": "st-1", "name": "stack-a"}
        adapter.client.stacks.read.assert_called_once_with("st-1")

    def test_not_found_returns_none(self):
        adapter = Mock()
        adapter.client.stacks.read.side_effect = NotFound("missing")
        assert get_stack(adapter, "st-missing") is None


class TestGetStackByName:
    def test_get_stack_by_name_returns_matching_stack(self):
        adapter = Mock()
        adapter.client.stacks.list.return_value = iter([
            _make_model({"id": "st-1", "name": "stack-a"}),
            _make_model({"id": "st-2", "name": "stack-b"}),
        ])

        stack = get_stack_by_name(adapter, "org", "stack-b")

        assert stack == {"id": "st-2", "name": "stack-b"}

    def test_get_stack_by_name_returns_none_when_missing(self):
        adapter = Mock()
        adapter.client.stacks.list.return_value = iter([
            _make_model({"id": "st-1", "name": "stack-a"}),
        ])

        assert get_stack_by_name(adapter, "org", "stack-x") is None


class TestCreateStack:
    @patch(f"{MU_PATH}.safe_api_call")
    @patch(f"{MU_PATH}.StackCreateOptions")
    def test_create_uses_sdk_options(self, mock_opts_cls, mock_safe_call):
        adapter = Mock()
        opts = Mock()
        mock_opts_cls.model_validate.return_value = opts
        mock_safe_call.return_value = _make_model({"id": "st-1", "name": "stack-a"})

        data = {"name": "stack-a", "project": {"id": "prj-1"}}
        result = create_stack(adapter, data)

        mock_opts_cls.model_validate.assert_called_once_with(data)
        args, kwargs = mock_safe_call.call_args
        assert args[0] is adapter.client.stacks.create
        assert args[1] is opts
        assert "error_context" in kwargs
        assert result == {"id": "st-1", "name": "stack-a"}


class TestUpdateStack:
    @patch(f"{MU_PATH}.safe_api_call")
    @patch(f"{MU_PATH}.StackUpdateOptions")
    def test_update_uses_sdk_options(self, mock_opts_cls, mock_safe_call):
        adapter = Mock()
        opts = Mock()
        mock_opts_cls.model_validate.return_value = opts
        mock_safe_call.return_value = _make_model({"id": "st-1", "name": "stack-renamed"})

        data = {"name": "stack-renamed"}
        result = update_stack(adapter, "st-1", data)

        mock_opts_cls.model_validate.assert_called_once_with(data)
        args, kwargs = mock_safe_call.call_args
        assert args[0] is adapter.client.stacks.update
        assert args[1] == "st-1"
        assert args[2] is opts
        assert result == {"id": "st-1", "name": "stack-renamed"}


class TestDeleteStack:
    @patch(f"{MU_PATH}.safe_api_call")
    def test_delete(self, mock_safe_call):
        adapter = Mock()
        delete_stack(adapter, "st-1")
        args, kwargs = mock_safe_call.call_args
        assert args[0] is adapter.client.stacks.delete
        assert args[1] == "st-1"
        assert "error_context" in kwargs
