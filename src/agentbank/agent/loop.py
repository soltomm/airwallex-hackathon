"""Manual tool-use loop on Claude Opus 5.5.

A manual loop (rather than the beta tool runner) keeps every step visible for the demo
and lets approvals pause inside a tool handler. Server-side refusal fallbacks are on.
"""
from __future__ import annotations

import anthropic

from .tools import TOOLS, AgentContext, run_tool

MODEL = "claude-opus-5-5"
MAX_TURNS = 25

SYSTEM = """You are a finance operations agent working on an Airwallex sandbox account.
Amounts are in major units (100 means one hundred dollars).

How you work:
- Read the current state with tools before you plan. Never assume a balance.
- State your plan briefly, then act. When new information changes the picture (a deposit,
  a failed transfer, a contradicting email), say what changed and revise the plan.
- You can only move money through pay_beneficiary. Policy code decides whether a payment
  runs, needs a person's approval, or is rejected; respect its answer and explain it.
- If a tool says a result is unknown or already handled, do not try to pay again.
- Escalate to a human when evidence conflicts or no option satisfies the policy.
- Finish with a short summary: what you did, what you deferred or escalated, and the
  resulting balances."""


def run_agent(goal: str, ctx: AgentContext, client: anthropic.Anthropic | None = None,
              on_text=print) -> list:
    client = client or anthropic.Anthropic()
    messages: list = [{"role": "user", "content": goal}]

    for _ in range(MAX_TURNS):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        messages.append({"role": "assistant", "content": response.content})
        for block in response.content:
            if block.type == "text" and block.text.strip():
                on_text(block.text)

        if response.stop_reason == "refusal":
            on_text(f"[model declined: {getattr(response.stop_details, 'category', None)}]")
            break
        if response.stop_reason == "max_tokens":
            on_text("[stopped: max_tokens]")
            break
        if response.stop_reason == "pause_turn":
            continue
        if response.stop_reason != "tool_use":
            break

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            ctx.audit.record("tool_call", name=block.name, input=block.input)
            content, is_error = run_tool(ctx, block.name, block.input)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": content, "is_error": is_error})
        # All results for one assistant turn go back in a single user message.
        messages.append({"role": "user", "content": results})
    else:
        on_text(f"[stopped after {MAX_TURNS} turns]")
    return messages
