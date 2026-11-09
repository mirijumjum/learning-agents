"""Scripted model for offline tests. Behaves like a real client, tool calls included."""

from __future__ import annotations

import re

from agent_framework import BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, Message

_GOOD_TESTS = '''FILE: tests/test_order_service.py
```python
import pytest

from app.order_service import OrderService


def test_subtotal_adds_price_times_quantity():
    assert OrderService().subtotal([{"price": 10.0, "quantity": 2}, {"price": 5.0, "quantity": 1}]) == 25.0


def test_apply_discount():
    assert OrderService().apply_discount(100.0, 25) == 75.0


@pytest.mark.parametrize("percent", [-1, 101])
def test_apply_discount_rejects_invalid_percent(percent):
    with pytest.raises(ValueError):
        OrderService().apply_discount(100.0, percent)


def test_total_applies_discount_then_tax():
    assert OrderService(tax_rate=0.5).total([{"price": 100.0, "quantity": 1}], discount_percent=50) == 75.0
```
'''
_WRONG_TEST = '''FILE: tests/test_order_service.py
```python
from app.order_service import OrderService


def test_apply_discount():
    assert OrderService().apply_discount(100.0, 25) == 80.0
```
'''
_API_DOC = """# customer-portal API

## GET /customers

Returns all customers.

## GET /customers/{id}

Returns one customer. Responds 404 if it does not exist.

## POST /customers

Creates a customer from the JSON body.
"""


class MockDevClient(FunctionInvocationLayer, BaseChatClient):
    OTEL_PROVIDER_NAME = "mock"

    def __init__(self, role: str, flaky_tests: bool = False) -> None:
        super().__init__()
        self.role = role
        self.flaky_tests = flaky_tests

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):  # type: ignore[override]
        if stream:
            raise NotImplementedError("MockDevClient only supports non-streaming calls.")
        reply = Message(role="assistant", contents=[self._answer(messages)])
        return ChatResponse(messages=[reply], model="mock", usage_details={"input_token_count": 100, "output_token_count": 50, "total_token_count": 150})

    def _answer(self, messages: list[Message]) -> Content | str:
        prompt = "\n".join(m.text for m in messages if m.role == "user")
        if self.role == "router":
            from devassist.orchestrator import keyword_route

            return keyword_route(prompt)
        if self.role == "git":
            return self._git_turn(prompt, messages)
        if self.role == "test":
            return self._test_turn(prompt)
        if self.role == "docs":
            match = re.search(r"File (\S+):\n(.*)", prompt, re.S)
            if not match:
                return _API_DOC
            return "```python\n" + match.group(2).replace("def list_customers():\n", 'def list_customers():\n    """Return all customers."""\n') + "\n```"
        return "The errors began right after the latest deployment, so that deployment is the probable cause.\nSuggested fix: roll back and check the database connection settings."

    def _test_turn(self, prompt: str) -> str:
        if "Failed job log:" in prompt:
            return "CODE_WRONG: apply_discount divides the percentage by 10 instead of 100." if re.search(r"percent / 10\b", prompt) else _GOOD_TESTS
        return _WRONG_TEST if self.flaky_tests else _GOOD_TESTS

    @staticmethod
    def _git_turn(prompt: str, messages: list[Message]) -> Content | str:
        done = {c.call_id for m in messages for c in m.contents if c.type == "function_result"}
        calls: list[tuple[str, dict]] = []
        branch = re.search(r"branch `([^`]+)` from `([^`]+)`", prompt)
        project = re.search(r"(payments-api|customer-portal)", prompt)
        name = project.group(1) if project else "payments-api"
        if branch:
            calls.append(("create_branch", {"branch": branch.group(1), "ref": branch.group(2), "project": name}))
        if re.search(r"\bMRs?\b|merge requests?", prompt):
            calls.append(("list_merge_requests", {"project": name, "assigned_to_me": "assigned to me" in prompt}))
        for index, (tool, arguments) in enumerate(calls):
            call_id = f"call_{index}"
            if call_id not in done:
                return Content.from_function_call(call_id=call_id, name=tool, arguments=arguments)
        return "Done. See the links below." + (" Also see https://gitlab.example.com/invented/link" if calls else "")
