"""
The chat turn loop: LLM ⇄ tools, then commit a version if the geometry changed.

Any OpenAI-compatible endpoint with tool calling works:
  LLM_BASE_URL (default Groq), LLM_API_KEY (falls back to GROQ_API_KEY), LLM_MODEL.
"""

import asyncio
import json
import os
import re

import openai
from openai import AsyncOpenAI

from .. import cad_service as cad
from . import prompt, tools

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "12"))
TOOL_RESULT_CHARS = int(os.getenv("TOOL_RESULT_CHARS", "4000"))
RATE_LIMIT_WAIT = float(os.getenv("LLM_RATE_LIMIT_WAIT", "30"))  # max seconds to wait out a 429
# Rough cap on input tokens per request (Groq free tier allows 8k tokens/minute in total).
INPUT_TOKEN_BUDGET = int(os.getenv("LLM_INPUT_TOKEN_BUDGET", "5500"))
# gpt-oss models accept low|medium|high; empty disables the parameter for models that reject it.
REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low" if "gpt-oss" in LLM_MODEL else "")

_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY or "missing-key")


class LLMError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _tokens(obj):
    return len(json.dumps(obj, default=str)) // 3  # conservative estimate (~3-4 chars per token)


def _fit_budget(messages, tool_defs):
    """Shorten older tool results (oldest first) until the request fits INPUT_TOKEN_BUDGET."""
    over = _tokens(messages) + _tokens(tool_defs or []) - INPUT_TOKEN_BUDGET
    for m in messages[:-1]:
        if over <= 0:
            break
        if m["role"] == "tool" and len(m["content"]) > 300:
            over -= (len(m["content"]) - 300) // 3
            m["content"] = m["content"][:300] + " …[older result truncated]"


async def _complete(messages, tool_defs=None):
    _fit_budget(messages, tool_defs)
    kwargs = {"model": LLM_MODEL, "messages": messages, "temperature": 0.1}
    if REASONING_EFFORT:
        kwargs["reasoning_effort"] = REASONING_EFFORT
    if tool_defs:
        kwargs.update(tools=tool_defs, tool_choice="auto")
    last_err = None
    for attempt in range(3):
        try:
            return (await _client.chat.completions.create(**kwargs)).choices[0].message
        except openai.RateLimitError as e:
            # Per-minute token limits clear quickly: wait out short windows instead of failing the turn.
            wait = _retry_after(e)
            if attempt < 2 and wait is not None and wait <= RATE_LIMIT_WAIT:
                await asyncio.sleep(wait + 0.5)
                continue
            raise LLMError("The LLM provider's rate limit was reached. Wait a minute and try again.", 429)
        except openai.APIStatusError as e:
            if e.status_code == 413:
                raise LLMError("The request is larger than the LLM provider allows per minute (free-tier "
                               "token limit). Lower MODEL_SUMMARY_CHARS / LLM_HISTORY_MESSAGES in "
                               "backend/.env or use a provider with a higher limit.", 413)
            if isinstance(e, openai.AuthenticationError):
                raise LLMError("LLM API key is missing or invalid (set LLM_API_KEY or GROQ_API_KEY).", 500)
            if not isinstance(e, openai.BadRequestError):
                last_err = e
                continue
            # Some providers reject malformed tool calls ("tool_use_failed"); retry once with a nudge.
            last_err = e
            messages = messages + [{"role": "system", "content":
                                    "Your previous tool call was malformed. Call tools with valid JSON arguments "
                                    "that match the schema, or answer in plain text."}]
            kwargs["messages"] = messages
        except openai.APIError as e:
            last_err = e
    raise LLMError(f"LLM request failed: {last_err}")


def _retry_after(err):
    """Seconds to wait from a 429 response (Retry-After header or "try again in 7.5s"), or None if unknown."""
    try:
        value = err.response.headers.get("retry-after")
        if value is not None:
            return float(value)
    except Exception:
        pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", str(err))
    if not m:
        return None
    secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
    return secs + 60 * int(m.group(1) or 0)


def _assistant_dict(msg):
    d = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                           for tc in msg.tool_calls]
    return d


def _describe(records):
    ok = [r["message"].split("\n")[0] for r in records if r["ok"]]
    return "; ".join(ok)[:300] or "Edit"


async def run_turn(session_id, user_message, history):
    """Run one chat turn. `history` is a list of prior {role, content} messages (oldest first).

    Returns {"content", "operations", "state"}.
    """
    if not LLM_API_KEY:
        raise LLMError("No LLM API key configured (set LLM_API_KEY or GROQ_API_KEY in backend/.env).", 500)
    meta = cad.load(session_id)
    if meta is None:
        msg = await _complete([{"role": "system", "content": prompt.NO_MODEL_PROMPT}, *history,
                               {"role": "user", "content": user_message}])
        return {"content": msg.content or "", "operations": [], "state": None}

    async with cad.lock(session_id):
        meta = cad.require(session_id)  # re-read under the lock
        messages = [{"role": "system", "content": prompt.build_system(meta)}, *history,
                    {"role": "user", "content": user_message}]
        records, content = [], ""
        tool_defs = tools.tools_for(meta)
        try:
            for _ in range(MAX_STEPS):
                msg = await _complete(messages, tool_defs)
                if not msg.tool_calls:
                    content = msg.content or ""
                    break
                messages.append(_assistant_dict(msg))
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError as e:
                        result, rec = {"ok": False, "error": f"Arguments are not valid JSON: {e}"}, None
                    else:
                        result, rec = await tools.execute(meta, tc.function.name, args)
                    if rec is not None:
                        records.append(rec)
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps(result, default=str)[:TOOL_RESULT_CHARS]})
            else:
                content = "I stopped after too many steps. Here is what was done so far."
        except Exception:
            await cad.discard_uncommitted(meta)
            raise

        if meta.get("dirty"):
            await cad.commit_version(meta, _describe(records))
        else:
            cad.save(meta)  # registry may have changed during queries
        if not content and records:
            content = "\n".join(f"- {r['message']}" if r["ok"] else f"- Failed: {r['error']}" for r in records)
        return {"content": content, "operations": records, "state": cad.public_state(meta)}
