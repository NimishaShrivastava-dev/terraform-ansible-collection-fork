# -*- coding: utf-8 -*-

# Copyright IBM Corp. 2025, 2026
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from typing import Any, Dict, Optional

try:
    from pytfe.errors import NotFound
    from pytfe.models import StackCreateOptions, StackUpdateOptions
except ImportError:

    class NotFound(Exception):  # type: ignore[no-redef]
        pass

    class StackCreateOptions:  # type: ignore[no-redef]
        pass

    class StackUpdateOptions:  # type: ignore[no-redef]
        pass


from ansible_collections.hashicorp.terraform.plugins.module_utils.client import (
    TerraformClient,
)
from ansible_collections.hashicorp.terraform.plugins.module_utils.utils import (
    format_response,
    safe_api_call,
)


def get_stack(adapter: TerraformClient, stack_id: str) -> Optional[Dict[str, Any]]:
    """Read a single stack by its ID. Returns None if not found."""
    try:
        stack = adapter.client.stacks.read(stack_id)
        return format_response(stack)
    except NotFound:
        return None


def get_stack_by_name(adapter: TerraformClient, organization: str, name: str) -> Optional[Dict[str, Any]]:
    """
    Find a stack by name within an organization.
    """
    try:
        for s in adapter.client.stacks.list(organization):
            stack = format_response(s)
            if stack.get("name") == name:
                return stack
    except NotFound:
        return None
    return None


def create_stack(adapter: TerraformClient, data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a stack. data must include 'name' and 'project' (with 'id')."""
    options = StackCreateOptions.model_validate(data)
    response = safe_api_call(
        adapter.client.stacks.create,
        options,
        error_context=f"Failed to create stack {data.get('name')!r}",
    )
    return format_response(response)


def update_stack(adapter: TerraformClient, stack_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Update an existing stack by its ID."""
    options = StackUpdateOptions.model_validate(data)
    response = safe_api_call(
        adapter.client.stacks.update,
        stack_id,
        options,
        error_context=f"Failed to update stack {stack_id}",
    )
    return format_response(response)


def delete_stack(adapter: TerraformClient, stack_id: str) -> None:
    """Delete a stack by its ID."""
    safe_api_call(
        adapter.client.stacks.delete,
        stack_id,
        error_context=f"Failed to delete stack {stack_id}",
    )
