"""The AI part: a model on Amazon Bedrock investigates a spike with Cost Explorer tools.

A small agent loop: the model decides which tools to call, the loop runs them and feeds
the results back until the model writes its explanation. A hard cap on tool calls keeps
Cost Explorer charges (and latency) bounded.

Two backends, picked from MODEL_ID:
* Claude (`anthropic.*` ids): the Anthropic Messages API on Bedrock's bedrock-mantle endpoint.
* Anything else, such as Amazon Nova (`us.amazon.nova-2-lite-v1:0`, the default): the
  Bedrock Converse API. Nova works on AWS Free plan accounts, which can't use Claude.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date

import boto3

from tools import TOOL_SCHEMAS, run_tool

log = logging.getLogger(__name__)

MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
EFFORT = os.environ.get("EFFORT", "medium")
MAX_TOOL_CALLS = int(os.environ.get("MAX_TOOL_CALLS", "6"))
MAX_TURNS = MAX_TOOL_CALLS + 2

SYSTEM_PROMPT = """You are a FinOps assistant. You explain AWS cost spikes to a busy account owner \
who wants to know what happened and what to do, not how Cost Explorer works.

You have read-only Cost Explorer tools. Each call costs the owner money, so make at most \
{max_calls} calls, and request several independent calls in the same turn when you can. \
A good path is: break the top-moving service down by USAGE_TYPE for the spike day and the \
week before it, then check REGION or OPERATION, or the daily trend, only if that is not conclusive.

Write the final answer as plain text for an email (no Markdown headings or tables), under 200 words, \
in exactly these four parts:
What happened: one or two sentences with the dollar amounts.
Likely cause: the specific service, usage type and region, and what kind of activity usually produces that usage.
What to check: two or three concrete steps in the AWS console or CLI.
Is it expected? Say whether it looks like a one-off, a new steady cost, or runaway usage, and why.

Only use numbers that appear in the spike summary or in tool results. If the data does not \
explain the spike, say so plainly instead of guessing."""

_client = None   # Anthropic client (Claude backend)
_runtime = None  # bedrock-runtime client (Converse backend)


def is_claude(model_id: str) -> bool:
    return "anthropic." in model_id


def client():
    """Created lazily so tests can import this module without AWS settings."""
    global _client
    if _client is None:
        from anthropic import AnthropicBedrockMantle
        # Signs requests with the Lambda role's credentials (SigV4).
        _client = AnthropicBedrockMantle(aws_region=os.environ.get("BEDROCK_REGION") or None, max_retries=3)
    return _client


def runtime():
    global _runtime
    if _runtime is None:
        _runtime = boto3.client("bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION") or None)
    return _runtime


def _text(content) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


def _prompt(spike_summary: str, today: date) -> str:
    return (f"Today is {today.isoformat()}. The daily cost check flagged this:\n\n{spike_summary}\n\n"
            "Investigate and explain it.")


def explain_spike(spike_summary: str, today: date | None = None) -> str:
    today = today or date.today()
    if is_claude(MODEL_ID):
        return _explain_claude(spike_summary, today)
    return _explain_converse(spike_summary, today)


def _explain_claude(spike_summary: str, today: date) -> str:
    messages: list[dict] = [{"role": "user", "content": _prompt(spike_summary, today)}]
    system = SYSTEM_PROMPT.format(max_calls=MAX_TOOL_CALLS)
    calls_used = 0

    for _ in range(MAX_TURNS):
        response = client().messages.create(
            model=MODEL_ID,
            max_tokens=16000,
            system=system,
            tools=TOOL_SCHEMAS,
            output_config={"effort": EFFORT},
            messages=messages,
        )
        log.info("stop_reason=%s usage=%s", response.stop_reason, response.usage)

        if response.stop_reason == "refusal":
            raise RuntimeError("Model declined to answer")
        if response.stop_reason in ("end_turn", "stop_sequence", "max_tokens"):
            text = _text(response.content)
            if text:
                return text
            raise RuntimeError(f"Model returned no text (stop_reason={response.stop_reason})")

        # Keep the whole assistant turn (thinking + tool_use blocks) in the history.
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue

        results = []
        for block in (b for b in response.content if b.type == "tool_use"):
            if calls_used >= MAX_TOOL_CALLS:
                body, is_error = ('{"error": "Tool budget used up. Write your explanation now '
                                  'from the data you already have."}', True)
            else:
                calls_used += 1
                body, is_error = run_tool(block.name, block.input)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": body, "is_error": is_error})
        # All results for one turn go back in a single user message.
        messages.append({"role": "user", "content": results})

    raise RuntimeError("Agent did not finish within the turn limit")



# ---------- Converse API backend (Amazon Nova and other Bedrock models) ----------

CONVERSE_TOOLS = {"tools": [
    {"toolSpec": {"name": t["name"], "description": t["description"],
                  "inputSchema": {"json": t["input_schema"]}}}
    for t in TOOL_SCHEMAS
]}

_THINKING = re.compile(r"<thinking>.*?</thinking>", re.S)


def _converse_text(content: list[dict]) -> str:
    text = "\n".join(b["text"] for b in content if "text" in b)
    return _THINKING.sub("", text).strip()


def _explain_converse(spike_summary: str, today: date) -> str:
    messages: list[dict] = [{"role": "user", "content": [{"text": _prompt(spike_summary, today)}]}]
    system = [{"text": SYSTEM_PROMPT.format(max_calls=MAX_TOOL_CALLS)}]
    calls_used = 0

    for _ in range(MAX_TURNS):
        response = runtime().converse(
            modelId=MODEL_ID,
            system=system,
            messages=messages,
            toolConfig=CONVERSE_TOOLS,
            inferenceConfig={"maxTokens": 4096},
        )
        stop = response["stopReason"]
        content = response["output"]["message"]["content"]
        log.info("stop_reason=%s usage=%s", stop, response.get("usage"))

        if stop in ("guardrail_intervened", "content_filtered"):
            raise RuntimeError(f"Model output was blocked ({stop})")
        if stop != "tool_use":
            text = _converse_text(content)
            if text:
                return text
            raise RuntimeError(f"Model returned no text (stop_reason={stop})")

        messages.append({"role": "assistant", "content": content})
        results = []
        for block in (b["toolUse"] for b in content if "toolUse" in b):
            if calls_used >= MAX_TOOL_CALLS:
                body, is_error = ('{"error": "Tool budget used up. Write your explanation now '
                                  'from the data you already have."}', True)
            else:
                calls_used += 1
                body, is_error = run_tool(block["name"], block.get("input") or {})
            results.append({"toolResult": {"toolUseId": block["toolUseId"], "content": [{"text": body}],
                                           "status": "error" if is_error else "success"}})
        messages.append({"role": "user", "content": results})

    raise RuntimeError("Agent did not finish within the turn limit")
