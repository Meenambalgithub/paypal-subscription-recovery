"""LLM adapter. The agent talks to a Session; which provider is behind it is set in .env.

    LLM_PROVIDER=gemini   (default)  uses GEMINI_API_KEY, LLM_MODEL, LLM_FALLBACK_MODEL
    LLM_PROVIDER=fake                 scripted replies, for tests (no network, no cost)

Adding another provider means one more Session class here; the agent doesn't change.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from . import config

log = logging.getLogger("llm")


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def plain(value):
    """Turn tool results into plain JSON types. Postgres gives Decimal and datetime,
    which LLM SDKs can't serialise (found with real Gemini: 'Decimal is not JSON serializable')."""
    def default(o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return str(o)
    return json.loads(json.dumps(value, default=default))


class LLMUnavailable(RuntimeError):
    """Every model failed (quota, overload, network). The agent retries the case later."""


# ---------------------------------------------------------------- Gemini

class GeminiSession:
    RETRYABLE = {429, 500, 502, 503, 504}

    def __init__(self, client, models: list[str], system: str, tool_specs: list[dict], sleep=time.sleep):
        from google.genai import types
        self.types, self.client, self.models, self.sleep = types, client, models, sleep
        self.contents = []
        self.config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.3,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"],
                                          parameters_json_schema=t["parameters"])
                for t in tool_specs])],
            # We run the tools ourselves, after the guardrails: never let the SDK call them.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def start(self, text: str) -> Reply:
        self.contents.append(self.types.Content(role="user", parts=[self.types.Part(text=text)]))
        return self._call()

    def send_results(self, results: list[tuple[str, dict]]) -> Reply:
        parts = [self.types.Part.from_function_response(name=name, response={"result": plain(result)})
                 for name, result in results]
        self.contents.append(self.types.Content(role="user", parts=parts))
        return self._call()

    def _call(self) -> Reply:
        from google.genai import errors
        last = None
        for model in self.models:
            for attempt in range(2):
                try:
                    resp = self.client.models.generate_content(model=model, contents=self.contents,
                                                               config=self.config)
                except errors.APIError as e:
                    last = e
                    if e.code not in self.RETRYABLE:
                        break                       # e.g. 404 wrong model name: try the next model
                    log.warning("%s busy or over quota (%s), attempt %d", model, e.code, attempt + 1)
                    self.sleep(2 * (attempt + 1))
                    continue
                except Exception as e:              # network, or a request we built badly
                    log.warning("%s request failed before an answer: %r", model, e)
                    last = e
                    break
                return self._parse(resp)
        raise LLMUnavailable(f"all models failed: {last}")

    def _parse(self, resp) -> Reply:
        if not resp.candidates or not resp.candidates[0].content:
            return Reply()
        content = resp.candidates[0].content
        # Keep the model's turn exactly as returned: newer Gemini models attach
        # "thought signatures" to function calls and expect them back next turn.
        self.contents.append(content)
        text, calls = [], []
        for part in content.parts or []:
            if part.function_call:
                calls.append(ToolCall(part.function_call.name, dict(part.function_call.args or {})))
            elif part.text and not getattr(part, "thought", False):
                text.append(part.text)
        return Reply(" ".join(t.strip() for t in text if t.strip()), calls)


class GeminiLLM:
    def __init__(self, api_key: str | None = None, models: list[str] | None = None):
        from google import genai
        key = api_key or config.GEMINI_API_KEY
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set in .env")
        self.client = genai.Client(api_key=key)
        self.models = models or [m for m in (config.LLM_MODEL, config.LLM_FALLBACK_MODEL) if m]

    def session(self, system: str, tool_specs: list[dict]) -> GeminiSession:
        return GeminiSession(self.client, self.models, system, tool_specs)


# ---------------------------------------------------------------- fake (tests)

class FakeSession:
    def __init__(self, script: list, record: list):
        self.script, self.record = script, record        # shared: steps are consumed across runs

    def _next(self) -> Reply:
        if not self.script:
            return Reply("done")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, Reply):
            return step
        return Reply("", [ToolCall(n, a) for n, a in step])     # [(name, args), ...]

    def start(self, text: str) -> Reply:
        self.record.append(("start", text))
        return self._next()

    def send_results(self, results):
        self.record.append(("results", results))
        return self._next()


class FakeLLM:
    """script: list of steps; a step is [(tool, args), ...], a Reply, or an Exception to raise."""

    def __init__(self, script: list | None = None):
        self.script, self.record, self.sessions = script or [], [], 0

    def session(self, system: str, tool_specs: list[dict]) -> FakeSession:
        self.sessions += 1
        self.record.append(("system", system))
        return FakeSession(self.script, self.record)


def make_llm():
    if config.LLM_PROVIDER == "gemini":
        return GeminiLLM()
    if config.LLM_PROVIDER == "fake":
        return FakeLLM()
    raise RuntimeError(f"unknown LLM_PROVIDER={config.LLM_PROVIDER!r} (use gemini or fake)")
