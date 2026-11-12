"""The Gemini adapter, against a fake client (no network, no key)."""
import pytest
from google.genai import errors, types

from recovery.llm import GeminiSession, LLMUnavailable
from recovery.tools import TOOL_SPECS


def response(*parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=list(parts)))])


def call(name, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def generate_content(self, model, contents, config):
        self.calls.append((model, len(contents), config))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class FakeClient:
    def __init__(self, outcomes):
        self.models = FakeModels(outcomes)


def session(outcomes, models=("main", "backup")):
    client = FakeClient(outcomes)
    return GeminiSession(client, list(models), "system", TOOL_SPECS, sleep=lambda s: None), client


def busy():
    return errors.ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}})


def test_tool_calls_and_text_are_parsed():
    s, client = session([response(types.Part(text="I'll email them."),
                                  call("send_message", intent="FIX_PAYMENT", subject="s", body="b"))])
    reply = s.start("facts")
    assert reply.text == "I'll email them."
    assert reply.tool_calls[0].name == "send_message" and reply.tool_calls[0].args["intent"] == "FIX_PAYMENT"


def test_config_disables_automatic_tool_running_and_declares_every_tool():
    s, client = session([response(types.Part(text="ok"))])
    s.start("facts")
    config = client.models.calls[0][2]
    assert config.automatic_function_calling.disable is True
    names = [f.name for f in config.tools[0].function_declarations]
    assert names == [t["name"] for t in TOOL_SPECS]


def test_history_keeps_model_turn_and_sends_results():
    s, client = session([response(call("get_case")), response(types.Part(text="done"))])
    s.start("facts")
    reply = s.send_results([("get_case", {"state": "OPEN"})])
    assert reply.text == "done"
    roles = [c.role for c in s.contents]
    assert roles == ["user", "model", "user", "model"]           # model turns kept as returned
    fr = s.contents[2].parts[0].function_response
    assert fr.name == "get_case" and fr.response == {"result": {"state": "OPEN"}}


def test_busy_model_retries_then_falls_back():
    s, client = session([busy(), busy(), response(types.Part(text="from backup"))])
    assert s.start("facts").text == "from backup"
    assert [m for m, *_ in client.models.calls] == ["main", "main", "backup"]


def test_wrong_model_name_skips_straight_to_backup():
    not_found = errors.ClientError(404, {"error": {"code": 404, "message": "no such model", "status": "NOT_FOUND"}})
    s, client = session([not_found, response(types.Part(text="ok"))])
    assert s.start("facts").text == "ok"
    assert [m for m, *_ in client.models.calls] == ["main", "backup"]


def test_everything_failing_raises_llm_unavailable():
    s, _ = session([busy(), busy(), busy(), busy()])
    with pytest.raises(LLMUnavailable):
        s.start("facts")


def test_thought_parts_are_not_shown_as_text():
    s, _ = session([response(types.Part(text="secret reasoning", thought=True), types.Part(text="Visible."))])
    assert s.start("facts").text == "Visible."


def test_postgres_types_in_tool_results_are_sent_as_plain_json():
    """Real bug: get_case returns Decimal and datetime; the Gemini SDK refused to serialise them."""
    import json
    from datetime import datetime, timezone
    from decimal import Decimal
    s, _ = session([response(call("get_case")), response(types.Part(text="ok"))])
    s.start("facts")
    s.send_results([("get_case", {"amount_at_risk": Decimal("20.00"),
                                  "opened_at": datetime(2026, 11, 11, tzinfo=timezone.utc)})])
    sent = s.contents[2].parts[0].function_response.response
    assert json.dumps(sent)                                   # would raise TypeError before the fix
    assert sent["result"] == {"amount_at_risk": 20.0, "opened_at": "2026-11-11T00:00:00+00:00"}

