import json

from kernel.voice.toolcall_proxy import parse_calls

TOOLS = [{"type": "function", "function": {"name": "cancel_mind_task", "parameters": {"properties": {"task_id": {"type": "integer"}}}}},
         {"type": "function", "function": {"name": "local_time", "parameters": {"properties": {"timezone": {"type": "string"}}}}}]


def test_single_call_typed():
    text, calls = parse_calls('\n\n<function name="cancel_mind_task"><param name="task_id">12</param></function>', TOOLS)
    assert text == "" and len(calls) == 1
    assert calls[0]["function"]["name"] == "cancel_mind_task"
    assert json.loads(calls[0]["function"]["arguments"]) == {"task_id": 12}


def test_parallel_calls_and_text():
    out = ('Aye.\n<function name="local_time"><param name="timezone">Asia/Tokyo</param></function>\n'
           '<function name="local_time"><param name="timezone">Europe/London</param></function>')
    text, calls = parse_calls(out, TOOLS)
    assert text == "Aye."
    assert [json.loads(c["function"]["arguments"])["timezone"] for c in calls] == ["Asia/Tokyo", "Europe/London"]


def test_no_args_and_plain_text():
    assert json.loads(parse_calls('<function name="local_time"></function>', TOOLS)[1][0]["function"]["arguments"]) == {}
    assert parse_calls("Paris, Admiral.", TOOLS) == ("Paris, Admiral.", [])
