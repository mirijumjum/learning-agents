"""An offline, scripted chat client so you can run the workflow with no API key.

It is also the best way to see what a "chat client" is in Agent Framework: the
only thing the rest of the framework needs is ``_inner_get_response`` turning a
list of messages into a ``ChatResponse``. Swap this for OpenAI, Azure OpenAI or
Anthropic and *nothing else* in the workflow changes.

Composing with ``FunctionInvocationLayer`` gives us the same tool-calling loop
the real clients have: when we answer with a ``function_call`` content, the
layer executes the tool and calls us again with the result.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from agent_framework import BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, Message


@dataclass(frozen=True)
class Scenario:
    keywords: tuple[str, ...]
    first_code: str  # may contain a deliberate bug, to exercise the retry loop
    fixed_code: str
    tests: str


_PALINDROME_TESTS = '''\
import pytest
from solution import is_palindrome


@pytest.mark.parametrize("text", ["", "a", "racecar", "abba"])
def test_simple_palindromes(text):
    assert is_palindrome(text) is True


@pytest.mark.parametrize("text", ["ab", "python", "abca"])
def test_not_palindromes(text):
    assert is_palindrome(text) is False


def test_ignores_case():
    assert is_palindrome("RaceCar") is True


def test_ignores_spaces_and_punctuation():
    assert is_palindrome("A man, a plan, a canal: Panama!") is True
'''

_FIZZBUZZ_TESTS = '''\
import pytest
from solution import fizzbuzz


def test_first_fifteen():
    assert fizzbuzz(15) == [
        "1", "2", "Fizz", "4", "Buzz", "Fizz", "7", "8", "Fizz", "Buzz",
        "11", "Fizz", "13", "14", "FizzBuzz",
    ]


def test_zero_returns_empty_list():
    assert fizzbuzz(0) == []


@pytest.mark.parametrize("n, last", [(3, "Fizz"), (5, "Buzz"), (30, "FizzBuzz"), (7, "7")])
def test_last_item(n, last):
    assert fizzbuzz(n)[-1] == last


def test_negative_raises():
    with pytest.raises(ValueError):
        fizzbuzz(-1)
'''

_FIZZBUZZ_CODE = '''\
def fizzbuzz(n: int) -> list[str]:
    """Return the FizzBuzz sequence from 1 to n as strings. Raises ValueError if n < 0."""
    if n < 0:
        raise ValueError("n must be >= 0")
    result: list[str] = []
    for i in range(1, n + 1):
        word = ("Fizz" if i % 3 == 0 else "") + ("Buzz" if i % 5 == 0 else "")
        result.append(word or str(i))
    return result
'''

_CELSIUS_TESTS = '''\
import pytest
from solution import celsius_to_fahrenheit


@pytest.mark.parametrize("c, f", [(0, 32), (100, 212), (-40, -40), (37, 98.6)])
def test_known_values(c, f):
    assert celsius_to_fahrenheit(c) == pytest.approx(f)


def test_below_absolute_zero_raises():
    with pytest.raises(ValueError):
        celsius_to_fahrenheit(-300)
'''

_CELSIUS_CODE = '''\
ABSOLUTE_ZERO_C = -273.15


def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert Celsius to Fahrenheit. Raises ValueError below absolute zero (-273.15 C)."""
    if celsius < ABSOLUTE_ZERO_C:
        raise ValueError("temperature below absolute zero")
    return celsius * 9 / 5 + 32
'''

SCENARIOS: dict[str, Scenario] = {
    "palindrome": Scenario(
        keywords=("palindrome", "palindromo", "palíndromo"),
        # Bug on purpose: forgets to ignore case, spaces and punctuation.
        first_code='''\
def is_palindrome(text: str) -> bool:
    """Return True if text reads the same forwards and backwards, ignoring case, spaces and punctuation."""
    return text == text[::-1]
''',
        fixed_code='''\
def is_palindrome(text: str) -> bool:
    """Return True if text reads the same forwards and backwards, ignoring case, spaces and punctuation."""
    cleaned = [ch.lower() for ch in text if ch.isalnum()]
    return cleaned == cleaned[::-1]
''',
        tests=_PALINDROME_TESTS,
    ),
    "fizzbuzz": Scenario(keywords=("fizzbuzz", "fizz"), first_code=_FIZZBUZZ_CODE, fixed_code=_FIZZBUZZ_CODE, tests=_FIZZBUZZ_TESTS),
    "celsius": Scenario(
        keywords=("celsius", "fahrenheit", "temperature", "temperatura"),
        first_code=_CELSIUS_CODE,
        fixed_code=_CELSIUS_CODE,
        tests=_CELSIUS_TESTS,
    ),
}

DEMO_PROMPTS = {
    "palindrome": "A function is_palindrome(text) that ignores case, spaces and punctuation",
    "fizzbuzz": "A function fizzbuzz(n) returning the FizzBuzz sequence 1..n as strings; ValueError if n < 0",
    "celsius": "A function celsius_to_fahrenheit(celsius) that raises ValueError below absolute zero",
}


def find_scenario(text: str) -> Scenario | None:
    lowered = text.lower()
    for scenario in SCENARIOS.values():
        if any(k in lowered for k in scenario.keywords):
            return scenario
    return None


def _fence(code: str) -> str:
    return f"```python\n{code}```"


class MockChatClient(FunctionInvocationLayer, BaseChatClient):
    """Plays one role ("coder", "tester" or "runner") with canned answers."""

    OTEL_PROVIDER_NAME = "mock"

    def __init__(self, role: str, latency_seconds: float = 0.3) -> None:
        super().__init__()
        self.role = role
        self.latency_seconds = latency_seconds

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):  # type: ignore[override]
        if stream:
            raise NotImplementedError("MockChatClient only supports non-streaming calls.")
        await asyncio.sleep(self.latency_seconds)  # pretend we're thinking
        return ChatResponse(messages=[Message(role="assistant", contents=[self._answer(messages)])], model="mock")

    def _answer(self, messages: list[Message]) -> Content | str:
        prompt = "\n".join(m.text for m in messages if m.role == "user")

        if self.role == "runner":
            return self._runner_turn(messages[-1])

        scenario = find_scenario(prompt)
        if scenario is None:
            return "# The mock provider has no script for this request."
        if self.role == "coder":
            return _fence(scenario.fixed_code if "PREVIOUS ATTEMPT FAILED" in prompt else scenario.first_code)
        if self.role == "tester":
            return _fence(scenario.tests)
        raise ValueError(f"Unknown mock role: {self.role}")

    @staticmethod
    def _runner_turn(last: Message) -> Content | str:
        results = [c for c in last.contents if c.type == "function_result"]
        if not results:
            # First turn: ask the framework to call our tool.
            return Content.from_function_call(call_id="call_1", name="run_pytest", arguments={"test_file": "test_solution.py"})

        output = str(results[0].result)
        match = re.search(r"exit_code=(-?\d+)", output)
        exit_code = int(match.group(1)) if match else -1
        lines = [line for line in output.splitlines() if line.strip()]
        tally = lines[-1].strip("= ") if lines else "no output"
        if exit_code == 0:
            return f"All tests passed ({tally})."
        failing = sorted(set(re.findall(r"FAILED \S+::(\w+)", output)))
        hint = f" Failing tests: {', '.join(failing)}." if failing else ""
        return f"Tests failed ({tally}).{hint} The implementation does not satisfy those cases of the spec."
