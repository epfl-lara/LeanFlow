"""Pin live public stream ordering, bounded previews and authoritative completions."""

from agent.runtime.harness.observation_stream import (
    MAX_LIVE_CHARS,
    MAX_LIVE_ITEMS,
    NativeObservationStream,
)


def delta(text, item="i1", item_type="reasoning", field="text"):
    return {
        "type": "item.delta",
        "item_id": item,
        "item_type": item_type,
        "field": field,
        "delta": text,
    }


def completed(text, item="i1", item_type="reasoning", **fields):
    return {
        "type": "item.completed",
        "item": {"id": item, "type": item_type, "text": text, **fields},
    }


def test_fragments_accumulate_throttle_and_complete_without_losing_full_evidence():
    clock = [0.0]
    stream = NativeObservationStream("lemex", lambda: clock[0])
    stream.consume({"type": "harness.settings", "invocation_id": "invocation1"})
    first = stream.consume(delta("First "))[0]
    assert first[1]["text"] == "First "
    assert first[1]["partial"] is True
    assert first[1]["invocation_id"] == "invocation1"
    assert stream.consume(delta("second ")) == []
    clock[0] = 0.5
    assert stream.consume(delta("third"))[0][1]["text"] == "First second third"
    full = "long text " * 4000
    terminal = stream.consume(completed(full))[0]
    assert terminal[1]["text"] == full
    assert terminal[1]["partial"] is False
    assert stream.flush() == []


def test_summary_trace_and_invocations_stay_distinct():
    stream = NativeObservationStream("lemex")
    assert stream.consume(delta("summary"))[0][1]["reasoning_kind"] == "summary"
    assert stream.consume(delta("trace", field="raw_text"))[0][1]["reasoning_kind"] == "trace"
    terminal = stream.consume(completed("full summary", raw_text="full trace"))
    assert [item[1]["text"] for item in terminal] == ["full summary", "full trace"]
    assert stream.flush() == []
    assert NativeObservationStream("lemex").invocation_id != stream.invocation_id
    assert stream.consume(delta("encrypted", field="encrypted_content")) == []
    assert stream.consume(delta("user prompt", item_type="user_message")) == []


def test_cumulative_updates_replace_text_and_flush_interrupted_partial():
    stream = NativeObservationStream("codex", lambda: 0.0)
    stream.consume(delta("first"))
    stream.consume(
        {"type": "item.updated", "item": {"id": "i1", "type": "reasoning", "text": "replacement"}}
    )
    remaining = stream.flush()
    assert remaining[0][1]["text"] == "replacement"
    assert remaining[0][1]["partial"] is True


def test_command_progress_keeps_description_and_measured_elapsed_until_completion():
    clock = [10.0]
    stream = NativeObservationStream("lemex", lambda: clock[0])
    stream.consume(
        {
            "type": "item.started",
            "item": {
                "id": "cmd",
                "type": "command_execution",
                "command": "lake env lean Main.lean",
            },
        }
    )
    clock[0] = 12.0
    progress = stream.consume(delta("checked ", "cmd", "command_execution", "aggregated_output"))[
        0
    ][1]
    assert progress["arguments"] == "lake env lean Main.lean"
    assert progress["description"] == "Run a command in the agent workspace"
    assert progress["duration_seconds"] == 2.0
    assert progress["result"] == "checked "
    clock[0] = 13.0
    terminal = stream.consume(
        completed("", "cmd", "command_execution", aggregated_output="checked all", exit_code=0)
    )[0][1]
    assert terminal["duration_seconds"] == 3.0
    assert terminal["partial"] is False
    assert not stream.tools and not stream.flush()


def test_live_state_is_bounded_and_does_not_claim_acceptance():
    stream = NativeObservationStream("lemex")
    record = stream.consume(delta("x" * (MAX_LIVE_CHARS * 2)))[0][1]
    assert len(record["text"]) == MAX_LIVE_CHARS
    assert "accepted" not in record
    for index in range(MAX_LIVE_ITEMS * 2):
        stream.consume(delta("public text", item=str(index)))
    assert len(stream.pending) <= MAX_LIVE_ITEMS
    assert len(stream.published) <= MAX_LIVE_ITEMS


def test_authoritative_empty_completion_closes_old_live_channels():
    stream = NativeObservationStream("lemex", lambda: 0.0)
    stream.consume(delta("Old summary "))
    stream.consume(delta("pending summary"))
    terminal = stream.consume(completed("", raw_text="Final trace"))
    by_channel = {details["reasoning_kind"]: details for _, details in terminal}
    assert by_channel["summary"]["text"] == ""
    assert by_channel["summary"]["partial"] is False
    assert by_channel["trace"]["text"] == "Final trace"
    assert stream.flush() == []
    stream.consume(delta("message ", "answer", "agent_message"))
    stream.consume(delta("pending", "answer", "agent_message"))
    assert stream.consume(completed("", "answer", "agent_message"))[0][1]["partial"] is False
    assert stream.flush() == []


def test_malformed_native_snapshots_do_not_enter_live_state():
    stream = NativeObservationStream("lemex")
    assert stream.consume({"type": "item.completed", "item": {"type": [], "id": "i1"}}) == []
    assert stream.consume({"type": []}) == []
    assert stream.consume(delta("text", field=[])) == []
