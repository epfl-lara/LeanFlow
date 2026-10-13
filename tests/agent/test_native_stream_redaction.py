"""Verify additive public native streams never publish incomplete credentials."""

from __future__ import annotations

import json

import pytest

from agent.runtime.harness.stream_redaction import NativeStreamRedactor


def _delta(text: object, **metadata: object) -> dict:
    return {
        "type": "item.delta",
        "item_id": "item_0",
        "item_type": "reasoning",
        "field": "raw_text",
        "delta": text,
        "reasoning_kind": "trace",
        **metadata,
    }


def _text(records: list[dict]) -> str:
    return "".join(record.get("delta", "") for record in records)


def test_live_prose_preserves_additive_text_and_flushes_ordinary_final_word():
    stream = NativeStreamRedactor(())
    records = []
    assert stream.consume(_delta("Checking")) == []
    records.extend(stream.consume(_delta(" the induction ")))
    assert _text(records) == "Checking the induction "
    records.extend(stream.consume(_delta("hypothesis")))
    records.extend(stream.flush())
    assert _text(records) == "Checking the induction hypothesis"
    assert all(row["reasoning_kind"] == "trace" for row in records)
    assert stream.flush() == []


def test_known_environment_secret_split_across_tokens_is_masked_before_publication():
    secret = "opaque credential value"
    stream = NativeStreamRedactor((secret,))
    records = []
    for fragment in ("Inspecting opaque ", "credential", " value", " and continuing. "):
        records.extend(stream.consume(_delta(fragment)))
        assert "opaque" not in _text(records)
        assert "credential" not in _text(records)
        assert "value" not in _text(records)
    records.extend(stream.flush())
    assert "[REDACTED]" in _text(records)
    assert "Inspecting" in _text(records)
    assert "and continuing." in _text(records)


def test_exact_credentials_are_masked_before_nested_marker_substitutions():
    secret = "private-prefix Bearer private-value private-suffix"
    stream = NativeStreamRedactor((secret,))
    records = stream.consume(_delta("Before " + secret + "\nNext step. "))
    assert "private" not in _text(records)
    assert "Next step." in _text(records)


@pytest.mark.parametrize(
    "fragments,private_words",
    [
        (("API_KE", "Y ", "= opaque", "-value\nNext step. "), ("opaque", "-value")),
        (("Authorization: Bea", "rer opaque", "-value\nNext step. "), ("opaque", "-value")),
        (("Bea", "rer opaque", "-value\nNext step. "), ("opaque", "-value")),
        (('API_KEY="opaque ', 'credential value"\nNext step. '), ("opaque", "credential")),
        (('"apiKey": "opaque ', 'credential value"\nNext step. '), ("opaque", "credential")),
    ],
)
def test_split_generic_credential_markers_hold_context_until_complete(fragments, private_words):
    stream = NativeStreamRedactor(())
    records = []
    for fragment in fragments:
        records.extend(stream.consume(_delta(fragment)))
        assert all(word not in _text(records) for word in private_words)
    records.extend(stream.flush())
    assert "Next step." in _text(records)
    assert all(word not in _text(records) for word in private_words)


def test_completed_item_flushes_its_tail_before_full_authoritative_record():
    stream = NativeStreamRedactor(())
    stream.consume(_delta("Checking the induction"))
    complete = {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "reasoning", "text": "Summary", "raw_text": "Full trace"},
    }
    records = stream.consume(complete)
    assert [row["type"] for row in records] == ["item.delta", "item.completed"]
    assert records[0]["delta"] == "induction"
    assert records[1]["item"]["raw_text"] == "Full trace"
    assert stream.flush() == []


def test_interrupted_known_secret_prefix_is_suppressed_on_final_flush():
    stream = NativeStreamRedactor(("opaque-credential",))
    records = stream.consume(_delta("Check opaque-cre")) + stream.flush()
    assert "opaque" not in _text(records)
    assert _text(records) == "Check [REDACTED]"


@pytest.mark.parametrize(
    "credential",
    [
        "opaque credential value",
        'API_KEY="opaque credential value"',
        '"apiKey": "opaque credential value"',
    ],
)
def test_complete_credentials_spanning_token_boundary_are_not_partially_released(credential):
    stream = NativeStreamRedactor(("opaque credential value",))
    records = stream.consume(_delta("Before " + credential))
    assert "opaque" not in _text(records)
    records += stream.consume(_delta("\nNext step. "))
    assert "opaque" not in _text(records)
    assert "Next step." in _text(records)


def test_completed_text_cannot_restore_a_masked_streaming_bearer_credential():
    stream = NativeStreamRedactor(())
    original = {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "reasoning",
            "raw_text": "Bearer opaque-value\nPublic prose",
        },
    }
    records = stream.consume(original)
    assert "opaque-value" not in json.dumps(records)
    assert records[0]["item"]["raw_text"].endswith("Public prose")
    assert original["item"]["raw_text"].startswith("Bearer opaque-value")


def test_incomplete_private_key_block_is_never_released():
    stream = NativeStreamRedactor(())
    records = []
    for fragment in ("Before\n-----BEGIN RSA PRIVATE ", "KEY-----\nprivate", "-body\n"):
        records.extend(stream.consume(_delta(fragment)))
        assert "private" not in _text(records)
    records.extend(stream.flush())
    assert "Before\n" in _text(records)
    assert "private" not in _text(records)


def test_unrelated_items_and_fields_do_not_share_partial_token_state():
    stream = NativeStreamRedactor(("opaque-credential",))
    records = stream.consume(_delta("opaque-cre"))
    records += stream.consume(_delta("ordinary prose ", item_id="other"))
    records += stream.consume(_delta("summary words ", field="text"))
    assert _text(records) == "ordinary prose summary words "
    records += stream.flush()
    assert "opaque" not in _text(records)


@pytest.mark.parametrize(
    "metadata",
    [
        {"item_id": None},
        {"item_id": []},
        {"item_id": ""},
        {"item_type": {}},
        {"item_type": "unknown"},
        {"field": []},
        {"field": "unknown"},
        {"delta": {"private": "fragment"}},
    ],
)
def test_malformed_or_unknown_delta_metadata_is_suppressed(metadata):
    stream = NativeStreamRedactor(())
    assert stream.consume(_delta("unsafe fragment", **metadata)) == []
    assert stream.flush() == []


def test_unrecognized_reasoning_kind_and_extra_fields_never_enter_public_delta():
    stream = NativeStreamRedactor(())
    records = stream.consume(
        _delta("Public prose ", reasoning_kind={"raw": "private"}, secret="private")
    )
    assert _text(records) == "Public prose "
    assert "reasoning_kind" not in records[0]
    assert "private" not in json.dumps(records)


def test_changed_item_type_does_not_join_a_previous_partial_fragment():
    stream = NativeStreamRedactor(("opaque-credential",))
    stream.consume(_delta("opaque-cre", field="text"))
    assert stream.consume(_delta("dential ", field="text", item_type="agent_message")) == []
    assert stream.consume(_delta("continuation ", field="text")) == []
    assert "opaque" not in _text(stream.flush())


def test_pending_overflow_suppresses_the_remaining_field_until_completion():
    stream = NativeStreamRedactor((), max_pending_chars=20)
    records = stream.consume(_delta("x" * 50))
    assert "x" not in _text(records)
    assert stream.consume(_delta("secret suffix with whitespace ")) == []
    assert sum(len(field.text) for field in stream._pending.values()) <= 20
    records += stream.consume(
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "reasoning", "text": "Safe summary"},
        }
    )
    assert records[-1]["item"]["text"] == "Safe summary"
    assert stream.consume(_delta("Fresh public prose "))[0]["delta"] == "Fresh public prose "


def test_many_unfinished_items_have_a_bounded_state_budget():
    stream = NativeStreamRedactor((), max_fields=2, max_pending_chars=16)
    for index in range(20):
        stream.consume(_delta("unfinished", item_id=f"item_{index}"))
    assert len(stream._pending) == 2
    assert sum(len(field.text) for field in stream._pending.values()) <= 32
    assert len(stream.flush()) == 2
