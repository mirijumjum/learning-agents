"""Monthly cost estimate in Copilot Credits, with the official rates and our own workload assumptions.

Rates checked on 2026-11-10:
- https://learn.microsoft.com/microsoft-copilot-studio/requirements-messages-management
- https://learn.microsoft.com/power-platform/admin/pay-as-you-go-meters  (USD 0.01 per credit)
- https://www.microsoft.com/en-us/microsoft-365-copilot/pricing/copilot-studio  (USD 200 per 25,000 credits/month)

The workload is the Foundry guide's 20-developer month, scaled linearly. The credits
per request are our estimate of tool calls, not a measurement: run the backend for
a pilot month and replace them with what `devassist-copilot usage` reports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

GENERATIVE_ANSWER = 2
AGENT_ACTION = 5  # each tool call, topic or trigger in generative orchestration
FLOW_ACTIONS_PER_100 = 13
REASONING_PER_1000_TOKENS = 10  # added when a reasoning ("deep") model orchestrates
PAYG_USD_PER_CREDIT = 0.01
PACK_USD, PACK_CREDITS = 200.0, 25_000
M365_COPILOT_USD_PER_USER = 30.0  # per user per month, paid yearly

# Agent flow WaitForTestRun: trigger, variable, response and ~10 polls of (GetTestRun waiting 25 s + set status) for a 4-minute pipeline.
FLOW_ACTIONS_PER_WAIT = 24


@dataclass(frozen=True)
class Workload:
    """Monthly requests for 20 developers, from the Foundry guide's assumptions."""

    tests: int = 200
    docs: int = 100
    auto_reviews: int = 300  # GitLab webhook: propose tests when an MR opens; runs autonomously
    traces: int = 300
    git_ops: int = 1_000
    test_attempts: float = 1.5  # average CI runs per test request (max 3)


def credits_per_request(attempts: float) -> dict[str, float]:
    flow = FLOW_ACTIONS_PER_WAIT * FLOW_ACTIONS_PER_100 / 100
    per_attempt = AGENT_ACTION * 2 + flow  # SubmitTestFile + call the WaitForTestRun flow, plus its actions
    test = GENERATIVE_ANSWER + AGENT_ACTION * 2 + attempts * per_attempt  # StartTestSession + OpenTestMergeRequest
    return {
        "tests": test,
        "docs": GENERATIVE_ANSWER + AGENT_ACTION * 2,  # GetDocsSources + Open...MergeRequest
        "auto_reviews": test + AGENT_ACTION,  # plus the trigger
        "traces": GENERATIVE_ANSWER + AGENT_ACTION * 1.3,  # InvestigateIncident/GetFailedPipelineLog, an issue 30 % of the time
        "git_ops": GENERATIVE_ANSWER + AGENT_ACTION,
    }


@dataclass(frozen=True)
class Estimate:
    developers: int
    credits: int
    autonomous_credits: int  # never covered by Microsoft 365 Copilot licences
    payg_usd: float
    best_usd: float  # cheapest mix of packs and pay-as-you-go
    packs: int
    licensed_usd: float  # if every developer already has Microsoft 365 Copilot: only autonomous runs are billed


def estimate(developers: int, workload: Workload = Workload()) -> Estimate:
    scale = developers / 20
    per = credits_per_request(workload.test_attempts)
    volumes = {"tests": workload.tests, "docs": workload.docs, "auto_reviews": workload.auto_reviews,
               "traces": workload.traces, "git_ops": workload.git_ops}
    credits = round(sum(per[k] * v * scale for k, v in volumes.items()))
    autonomous = round(per["auto_reviews"] * workload.auto_reviews * scale)
    payg = credits * PAYG_USD_PER_CREDIT
    options = []
    for packs in range(0, math.ceil(credits / PACK_CREDITS) + 1):
        overage = max(credits - packs * PACK_CREDITS, 0)
        options.append((packs * PACK_USD + overage * PAYG_USD_PER_CREDIT, packs))
    best_usd, packs = min(options)
    return Estimate(developers, credits, autonomous, round(payg, 2), round(best_usd, 2), packs, round(autonomous * PAYG_USD_PER_CREDIT, 2))


def reasoning_surcharge_usd(million_tokens: float) -> float:
    """Extra cost if a reasoning model orchestrates: 10 credits per 1,000 billed tokens."""
    return million_tokens * 1_000 * REASONING_PER_1000_TOKENS * PAYG_USD_PER_CREDIT
