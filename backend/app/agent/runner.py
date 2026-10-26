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
from .. import precision
from . import fastpath, plans, prompt, tools

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "12"))
MAX_TOOL_FAILURES = int(os.getenv("AGENT_MAX_TOOL_FAILURES", "3"))  # then the model must answer in text
TOOL_RESULT_CHARS = int(os.getenv("TOOL_RESULT_CHARS", "4000"))
RATE_LIMIT_WAIT = float(os.getenv("LLM_RATE_LIMIT_WAIT", "65"))  # max seconds to wait out a 429 (one TPM window)
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


def _rejection(err):
    """The provider's reason for rejecting a request, plus the call it rejected, kept short."""
    body = getattr(err, "body", None)
    e = body.get("error", body) if isinstance(body, dict) else {}
    msg = (e.get("message") if isinstance(e, dict) else None) or str(err)
    failed = e.get("failed_generation") if isinstance(e, dict) else None
    return (msg + (f" Rejected call: {failed}" if failed else ""))[:600]


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
            print(f"[agent] rate limited (attempt {attempt + 1}); retry after {wait}s")
            if attempt < 2 and wait is not None and wait <= RATE_LIMIT_WAIT:
                await asyncio.sleep(wait + 0.5)
                continue
            daily = "per day" in str(e) or "TPD" in str(e)
            when = f"in about {round(wait / 60)} min" if wait and wait > 90 else "in a minute"
            raise LLMError(f"The LLM provider's {'daily' if daily else 'per-minute'} token limit was reached. "
                           f"Try again {when}" + (" (or use another API key / model in backend/.env)." if daily else ".")
                           + " Exact commands like `H2 Ø8` or `P5-P6 = 120` still work without the AI.", 429)
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
            # Some providers validate tool calls themselves and reject bad ones ("tool_use_failed"); retry
            # with the provider's reason so the model can fix the arguments instead of repeating them.
            last_err = e
            print(f"[agent] provider rejected the request (attempt {attempt + 1}): {_rejection(e)}")
            if kwargs.get("tools"):
                kwargs["tools"] = kwargs["tools"] + _unsent_tools(_rejection(e), kwargs["tools"])
            messages = messages + [{"role": "system", "content":
                                    "Your previous tool call was rejected: " + _rejection(e) + " Call the tool "
                                    "again with arguments that match its schema exactly (e.g. points are "
                                    "[x, y, z] with all three numbers), or answer in plain text."}]
            kwargs["messages"] = messages
        except openai.APIError as e:
            last_err = e
    if isinstance(last_err, openai.BadRequestError):
        raise LLMError("The AI made an invalid tool call three times, so nothing was changed. Try rephrasing the "
                       f"request. (Provider said: {_rejection(last_err)[:300]})", 502)
    raise LLMError(f"LLM request failed: {last_err}")


def _unsent_tools(text, sent):
    """Definitions of known tools the model tried to call although they weren't sent (e.g. draft_faces before
    load_tools "modeling"), so the retry can offer them."""
    have = {t["function"]["name"] for t in sent}
    named = set(re.findall(r"[a-z_]+", text or ""))
    return [t for t in tools.TOOLS if t["function"]["name"] in named - have
            and (tools.ALLOW_PYTHON or t["function"]["name"] != "run_freecad_python")]


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


_ID_RE = re.compile(r"\b(PAT\d+|SL\d+|PK\d+|CH\d+|B\d+|E\d+|[HCRPKSF]\d+(?:[-∠◎][HCRPKS]\d+|\.(?:Ø|R))?)(?!\w)")
MAX_HIGHLIGHT = 12
_NOTE_RE = re.compile(r"^\s*\[(?:Edits applied|Model version|Selected)[^\n]*\]\s*$", re.M)


_CLAIM_RE = re.compile(r"(✓|\bverified\b|\b(?:resized|added|moved|filleted|rounded|removed|increased|changed|set)\b"
                       r"[^\n]{0,80}\d)", re.I)


def _guard_unverified_claims(content, records, extra):
    """The model must not report edits it didn't make. When a turn made no edit (and proposed no plan) but the reply
    reads like edit results, say so plainly instead of passing the claims on."""
    if records or extra.get("plan") or extra.get("choices") or extra.get("options") or not content:
        return content
    if not _CLAIM_RE.search(content):
        return content
    print("[agent] reply claimed edits without any tool call; replaced")
    return ("⚠ No change was made: the assistant described edits it did not actually run, so its reply was withheld. "
            "Please send the request again (or split it into single steps).")


def _highlight_ids(meta, records, content):
    """Feature / dimension / pattern IDs to highlight: those the edits touched, then those named in the reply."""
    info = meta.get("info") or {}
    known = {f["id"] for fs in (info.get("features") or {}).values() for f in fs}
    known |= {p["id"] for p in info.get("patterns") or []}
    known |= {d["id"] for d in info.get("dimensions") or []}
    known |= {o["id"] for o in info.get("objects") or [] if o.get("id")}
    edges = set(((meta.get("registry") or {}).get("edges") or {}).keys())
    out = []

    def add(x):
        if not isinstance(x, str):
            return
        x = x.strip().upper()
        parts = precision.dim_refs(x)
        if re.fullmatch(r"E\d+", x) and x in edges and x not in out:
            out.append(x)
            return
        if (x in known or (len(parts) == 2 and all(p in known for p in parts))) and x not in out:
            out.append(x)

    for r in records:
        if not r.get("ok"):
            continue
        for fid in r.get("new_features") or []:
            add(fid)
        for k in ("hole", "face", "reference_face", "dimension", "pattern"):
            add((r.get("args") or {}).get(k))
        for fid in (r.get("args") or {}).get("features") or []:
            add(fid)
        for k in ("slot", "pocket", "chamfer"):
            add((r.get("args") or {}).get(k))
        for sel in (r.get("args") or {}).get("edges") or []:
            if isinstance(sel, dict):
                add(sel.get("edge"))
        for v in r.get("verified") or []:
            if v.get("status") == "ok":
                add(v.get("id"))
    for m in _ID_RE.finditer(content or ""):
        add(m.group(1))
    return out[:MAX_HIGHLIGHT]


async def _fast_turn(meta, parsed):
    name, args = parsed
    result, rec = await tools.execute(meta, name, args)
    records = [rec] if rec is not None else []
    return fastpath.reply(result, precision.settings_of(meta)), records


def _fast_parse(meta, text, selection):
    """Fast-path parse with the session's display unit (bare numbers) and its angle dimensions."""
    if meta is None:
        return None
    unit = precision.settings_of(meta)["display_unit"]
    return fastpath.parse(text, selection, unit, lambda did: tools.is_angle_dimension(meta.get("info"), did))


_EDIT_VERBS = re.compile(
    r"\b(make|set|change|increase|decrease|reduce|extend|lengthen|shorten|resize|enlarge|add|drill|bore|tap|cut|"
    r"round|fillet|chamfer|bevel|move|shift|remove|delete|fill|mirror|pattern|copy|shell|hollow|split|draft|rotate|"
    r"scale|thicken|widen|deepen|counterbore|countersink)\b", re.I)


def is_compound(text):
    """A request for two or more different edits ("make it 150 long, add two holes and round the corners"):
    its edits are collected into a plan the user approves instead of being applied one by one."""
    verbs = {m.group(1).lower() for m in _EDIT_VERBS.finditer(text or "")}
    clauses = [c for c in re.split(r",|;|\band\b|\bthen\b", text or "", flags=re.I) if _EDIT_VERBS.search(c)]
    return len(clauses) >= 2 and len(verbs) >= 2


_OPEN_ENDED = re.compile(
    r"\b(smooth(?:er|en)?|sleek(?:er)?|nicer|better|improve|stronger|stiffer|lighter|reduce (?:the )?weight|"
    r"aerodynamic|streamline[d]?|ergonomic|softer|round(?:er)? (?:it )?off|clean(?:er)? up|refine|optimi[sz]e|"
    r"modern|elegant|blunt(?:er)?|sharper|options|alternatives|(?:different|other) ways)\b", re.I)


def is_open_ended(text):
    """A qualitative request with no exact values ("make the nose end smoother"): several edits could satisfy it,
    so the agent offers alternatives for the user to pick instead of choosing one itself."""
    return bool(_OPEN_ENDED.search(text or "")) and not re.search(r"\d", text or "")


_OPTION_PICK = re.compile(r"^\s*(?:(?:apply|use|go with|pick|choose|take)\s+)?(?:(option|alternative|choice|#)\s*)?"
                          r"(\d)\s*(?:please)?\s*[.!]*\s*$", re.I)
_APPLY_WORDS = re.compile(r"^\s*(apply|apply it|yes|yes please|go ahead|do it|ok|okay|confirm|approve|apply the plan)\s*[.!]*\s*$", re.I)
_CANCEL_WORDS = re.compile(r"^\s*(cancel|no|discard|don'?t|stop|cancel the plan)\s*[.!]*\s*$", re.I)
_PREVIEW = re.compile(r"^\s*preview\s*:?\s+(.+)$", re.I)


async def _plan_turn(meta, user_message, selection):
    """Plan control without the LLM: approve / cancel the pending plan, or 'preview <command>'.
    Returns (content, records, extra) or None."""
    offer = meta.get("options")
    m = _OPTION_PICK.match(user_message)
    if offer and offer.get("base_version") == meta["head"] and m and (m.group(1) or not selection):
        content, records, _ = await plans.apply_option(meta, int(m.group(2)))
        return content, records, {}
    if offer and _CANCEL_WORDS.match(user_message):
        plans.cancel(meta)
        return "Dropped the options; nothing was changed.", [], {}
    if meta.get("pending") and _APPLY_WORDS.match(user_message):
        content, records, _ = await plans.apply(meta)
        return content, records, {}
    if meta.get("pending") and _CANCEL_WORDS.match(user_message):
        plans.cancel(meta)
        return "Cancelled the plan; nothing was changed.", [], {}
    m = _PREVIEW.match(user_message)
    if m:
        parsed = _fast_parse(meta, m.group(1), selection)
        if parsed and parsed[0] in tools.EDIT_TOOLS:
            pending = await plans.preview(meta, [{"tool": parsed[0], "args": parsed[1]}], source="preview command")
            return plans.describe(pending), [], {"plan": pending}
    return None


async def run_tool(session_id, tool, args, note=None):
    """Apply one tool call outside the chat loop (a DFM fix, a quick action) as its own version."""
    async with cad.lock(session_id):
        meta = cad.require(session_id)
        await cad.sync_manual_edits(meta)
        try:
            result, rec = await tools.execute(meta, tool, dict(args or {}))
        except Exception:
            await cad.discard_uncommitted(meta)
            raise
        if meta.get("dirty"):
            await cad.commit_version(meta, (note or _describe([rec] if rec else [])))
        else:
            cad.save(meta)
        records = [rec] if rec else []
        return {"content": fastpath.reply(result, precision.settings_of(meta)), "operations": records,
                "state": cad.public_state(meta), "highlight": _highlight_ids(meta, records, "")}


async def run_turn(session_id, user_message, history, selection=None):
    """Run one chat turn. `history` is a list of prior {role, content} messages (oldest first).
    `selection`: IDs the user selected in the viewer; the request applies to them.

    Returns {"content", "operations", "state", "highlight", "plan"?, "choices"?}.
    """
    meta = cad.load(session_id)
    parsed = _fast_parse(meta, user_message, selection)
    plan_cmd = meta is not None and (bool(_PREVIEW.match(user_message)) or (bool(meta.get("pending")) and bool(
        _APPLY_WORDS.match(user_message) or _CANCEL_WORDS.match(user_message))) or (bool(meta.get("options")) and bool(
        _OPTION_PICK.match(user_message) or _CANCEL_WORDS.match(user_message))))
    if not LLM_API_KEY and parsed is None and not plan_cmd:
        raise LLMError("No LLM API key configured (set LLM_API_KEY or GROQ_API_KEY in backend/.env).", 500)
    if meta is None:
        msg = await _complete([{"role": "system", "content": prompt.NO_MODEL_PROMPT}, *history,
                               {"role": "user", "content": user_message}])
        return {"content": msg.content or "", "operations": [], "state": None, "highlight": []}

    async with cad.lock(session_id):
        meta = cad.require(session_id)  # re-read under the lock
        await cad.sync_manual_edits(meta)
        records, content, extra = [], "", {}
        try:
            parsed = _fast_parse(meta, user_message, selection)  # meta re-read under the lock
            planned = await _plan_turn(meta, user_message, selection)
            if planned is not None:
                content, records, extra = planned
            elif parsed is not None:
                content, records = await _fast_turn(meta, parsed)
            else:
                messages = [{"role": "system", "content": prompt.build_system(meta)}, *history,
                            {"role": "user", "content": prompt.with_selection(user_message, meta.get("info"),
                                                                              selection or [])}]
                groups = set(tools.groups_for_message(user_message))
                tool_defs = tools.tools_for(meta, groups)
                failures = 0
                plan_mode = is_compound(user_message)
                options_mode = not plan_mode and is_open_ended(user_message)
                if options_mode:
                    messages.append({"role": "system", "content": (
                        "This request is open-ended. Look up what you need, then call propose_options with 2-4 "
                        "different alternatives (exact values); do not apply an edit directly.")})
                option_retries = 1  # alternatives that all fail go back to the model once
                tried = []  # (tool, error) of failed calls, for the reply when the model gives up
                queued = []  # plan mode: edits collected for one previewed plan
                plan_retries = 1  # a failed plan preview goes back to the model once before the user sees it
                steps_left = MAX_STEPS
                while steps_left > 0:
                    steps_left -= 1
                    # After repeated failed edits, stop offering tools so the model explains the problem
                    # instead of burning the token budget on near-identical retries.
                    try:
                        msg = await _complete(messages, tool_defs if failures < MAX_TOOL_FAILURES else None)
                    except LLMError:
                        if failures < MAX_TOOL_FAILURES:
                            raise
                        # Tools were withdrawn after repeated failures and the model still won't answer in text.
                        content = _gave_up(tried)
                        break
                    if not msg.tool_calls:
                        content = msg.content or ""
                        if queued and plan_retries and steps_left > 0:
                            pending = await plans.preview(meta, queued, (content or user_message).strip()[:200],
                                                          source="compound request")
                            if not pending.get("ok"):
                                plan_retries -= 1
                                queued, note = _plan_retry(pending)
                                messages.append({"role": "assistant", "content": content})
                                messages.append({"role": "user", "content": note})
                                continue
                            content, extra = plans.describe(pending), {"plan": pending}
                        break
                    calls = []
                    for tc in msg.tool_calls:
                        try:
                            calls.append((tc, json.loads(tc.function.arguments or "{}"), None))
                        except json.JSONDecodeError as e:
                            calls.append((tc, None, f"Arguments are not valid JSON: {e}"))
                    # Ends the turn: a plan / question for the user, or large edits shown as a preview first.
                    stop = await _stop_calls(meta, msg, calls, plan_mode, queued)
                    if stop is not None:
                        content, extra = stop
                        offer = extra.get("options")
                        if offer and not any(o["ok"] for o in offer["options"]) and option_retries and steps_left > 0:
                            option_retries -= 1  # every alternative failed its dry run: the model gets one more go
                            note = ("None of the alternatives worked, nothing was changed: " + "; ".join(
                                f"{o['title']}: {o['error']}" for o in offer["options"]) + ". Call propose_options "
                                "again with alternatives that avoid these errors (other features, edges or tools).")
                            extra = {}
                            messages.append(_assistant_dict(msg))
                            for tc, _a, _e in calls:
                                messages.append({"role": "tool", "tool_call_id": tc.id,
                                                 "content": json.dumps({"ok": False, "error": note})[:TOOL_RESULT_CHARS]})
                            continue
                        pend = extra.get("plan")
                        if plan_mode and pend and not pend.get("ok") and plan_retries and steps_left > 0:
                            plan_retries -= 1  # the model corrects a failed plan once before the user sees it
                            (queued, note), extra = _plan_retry(pend), {}
                            messages.append(_assistant_dict(msg))
                            for tc, _a, _e in calls:
                                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(
                                    {"ok": False, "error": note})[:TOOL_RESULT_CHARS]})
                            continue
                        break
                    messages.append(_assistant_dict(msg))
                    for tc, args, err in calls:
                        if err:
                            result, rec = {"ok": False, "error": err}, None
                        elif options_mode and tc.function.name in tools.EDIT_TOOLS:
                            result, rec = {"ok": False, "error": (
                                "Not applied: this request is open-ended. Offer this edit as one alternative of "
                                "propose_options (2-4 alternatives) so the user can choose.")}, None
                        elif plan_mode and tc.function.name in tools.EDIT_TOOLS:
                            queued.append({"tool": tc.function.name, "args": args})
                            result, rec = {"ok": True, "queued": True, "note": (
                                f"Queued as step {len(queued)} of a plan (not applied yet). Add the remaining edits the "
                                "same way; when you are done, reply in one short line and the plan is previewed for the "
                                "user to approve.")}, None
                        elif tc.function.name == "load_tools":
                            g = str((args or {}).get("group", ""))
                            groups.add(g)
                            tool_defs = tools.tools_for(meta, groups)
                            result, rec = {"ok": True, "loaded": tools.GROUPS.get(g, [])}, None
                        else:
                            result, rec = await tools.execute(meta, tc.function.name, args)
                        print(f"[agent] {tc.function.name} {json.dumps(args, default=str)[:300]} -> "
                              f"{'ok' if result.get('ok', True) else 'FAILED: ' + str(result.get('error'))[:200]}")
                        if rec is not None:
                            records.append(rec)
                        if not result.get("ok", True):
                            failures += 1
                            tried.append((tc.function.name, str(result.get("error"))[:200]))
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "content": json.dumps(result, default=str)[:TOOL_RESULT_CHARS]})
                else:
                    content = "I stopped after too many steps. Here is what was done so far."
                if queued and not extra:
                    pending = await plans.preview(meta, queued, (content or user_message).strip()[:200], source="compound request")
                    content, extra = plans.describe(pending), {"plan": pending}
        except Exception:
            await cad.discard_uncommitted(meta)
            raise

        if meta.get("dirty"):
            await cad.commit_version(meta, _describe(records))
        else:
            cad.save(meta)  # registry may have changed during queries
        # Models sometimes copy the bookkeeping notes appended to history messages; drop them.
        content = _NOTE_RE.sub("", content or "").strip()
        content = _guard_unverified_claims(content, records, extra)
        if not content and records:
            content = "\n".join(f"- {r['message']}" if r["ok"] else f"- Failed: {r['error']}" for r in records)
        hl = _highlight_ids(meta, records, content)
        if extra.get("choices"):
            hl = [c["id"] for c in extra["choices"] if isinstance(c, dict) and c.get("id")][:MAX_HIGHLIGHT]
        return {"content": content, "operations": records, "state": cad.public_state(meta), "highlight": hl, **extra}


def _gave_up(tried):
    """Reply when the model stopped after repeated failed tool calls: what was tried and why it failed."""
    lines = ["I couldn't make that change; nothing was modified. What I tried:"]
    seen = set()
    for name, err in tried:
        if (name, err) not in seen:
            seen.add((name, err))
            lines.append(f"- `{name}`: {err}")
    lines.append("Try describing the change differently, select the face or edge you mean, or ask "
                 "\"what are my options to ...\" to get alternatives.")
    return "\n".join(lines[:8])


def _plan_retry(pending):
    """(steps kept, note for the model) after a plan preview failed at step N: steps 1..N-1 previewed fine and stay
    queued; the model is asked only for what replaces step N onwards, keeping the user's values."""
    steps = [{"tool": st["tool"], "args": st["args"]} for st in pending.get("steps") or []]
    m = re.match(r"step (\d+)", str(pending.get("error") or ""), re.I)
    n = int(m.group(1)) if m else 1
    kept = steps[:n - 1]
    done = "; ".join(plans.label(st) for st in kept) or "none"
    return kept, (f"The plan preview failed: {pending.get('error')}. Nothing was changed. Steps kept (they worked, "
                  f"do not send them again): {done}. Call the edit tools for step {n} onwards again, corrected (for "
                  "example other feature or face IDs or edge selectors) but with the values the user asked for; "
                  "then reply in one line.")


async def _stop_calls(meta, msg, calls, plan_mode=False, queued=()):
    """(content, extra) when this LLM step ends the turn: propose_plan, ask_user, or edits big enough to preview.
    In plan mode edits are queued by the caller (they all end up in one plan), so they never stop the turn here."""
    by_name = {tc.function.name: args for tc, args, err in calls if not err}
    if "ask_user" in by_name:
        a = by_name["ask_user"] or {}
        choices = [{"id": str(c.get("id", "")).upper(), "label": str(c.get("label", ""))}
                   for c in a.get("choices") or [] if isinstance(c, dict)]
        return a.get("question") or "Which one do you mean?", {"choices": choices}
    if "propose_options" in by_name:
        a = by_name["propose_options"] or {}
        opts = [o for o in a.get("options") or [] if isinstance(o, dict)]
        if not opts:
            return "I couldn't put alternatives together for that; please describe the change in more detail.", {}
        offer = await plans.propose_options(meta, a.get("question") or "", opts)
        return plans.describe_options(offer), {"options": offer}
    if "propose_plan" in by_name:
        a = by_name["propose_plan"] or {}
        steps = [{"tool": st.get("tool"), "args": st.get("args") or {}} for st in a.get("steps") or [] if isinstance(st, dict)]
        steps = [q for q in queued if q not in steps] + steps  # edits queued earlier in this turn come first
        try:
            pending = await plans.preview(meta, steps, a.get("summary") or "")
        except cad.CadError as e:
            return f"I couldn't prepare that plan: {e}", {}
        return plans.describe(pending), {"plan": pending}
    edits = [{"tool": tc.function.name, "args": args} for tc, args, err in calls
             if not err and tc.function.name in tools.EDIT_TOOLS]
    if not plan_mode and edits and len(edits) == len(calls) and plans.is_large(edits):
        pending = await plans.preview(meta, edits, (msg.content or "").strip()[:200], source="large edit")
        return plans.describe(pending), {"plan": pending}
    return None
