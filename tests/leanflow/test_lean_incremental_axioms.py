from __future__ import annotations

from leanflow_cli.lean import lean_incremental_axioms as inline_axioms


def _query():
    query = inline_axioms.build_inline_axiom_query(
        "theorem demo : True := by\n  trivial",
        target="demo",
        requested_target="Demo.demo",
    )
    assert query is not None
    return query


def test_inline_axiom_query_wraps_exact_declaration_with_isolated_markers():
    query = _query()

    assert query.source.startswith("theorem demo : True := by\n  trivial\n")
    assert f"#print axioms {query.target}" in query.source
    assert query.source.count(query.begin_marker) == 1
    assert query.source.count(query.end_marker) == 1
    assert len(query.declaration_sha256) == 64


def test_inline_axiom_query_precedes_trailing_namespace_closures():
    """Keep a dotted declaration query in the namespace that declares it."""
    target = "erdos_242.variants.schinzel_generalization"
    query = inline_axioms.build_inline_axiom_query(
        "\n".join(
            (
                f"theorem {target} : True := by",
                "  trivial",
                "",
                "end Inner",
                "end Erdos242",
            )
        ),
        target=target,
        requested_target=target,
    )

    assert query is not None
    print_index = query.source.index(f"#print axioms {target}")
    assert print_index < query.source.index("end Inner")
    assert print_index < query.source.index("end Erdos242")


def test_inline_axiom_parser_returns_exact_dependencies_and_message_range():
    query = _query()
    messages = [
        {"severity": "warning", "message": "proof warning"},
        {"severity": "information", "message": f'"{query.begin_marker}" : String'},
        {
            "severity": "information",
            "message": "'Demo.demo' depends on axioms: [propext, Classical.choice]",
        },
        {"severity": "information", "message": f'"{query.end_marker}" : String'},
    ]

    profile, error = inline_axioms.parse_inline_axiom_messages(messages, query)

    assert error == ""
    assert profile is not None
    assert profile.axioms == ("Classical.choice", "propext")
    assert profile.message_start == 1
    assert profile.message_end == 3


def test_inline_axiom_parser_accepts_explicit_axiom_free_output():
    query = _query()
    messages = [
        {"message": query.begin_marker},
        {"message": "'Demo.demo' does not depend on any axioms"},
        {"message": query.end_marker},
    ]

    profile, error = inline_axioms.parse_inline_axiom_messages(messages, query)

    assert error == ""
    assert profile is not None
    assert profile.axioms == ()


def test_inline_axiom_parser_rejects_missing_duplicate_and_ambiguous_evidence():
    query = _query()
    missing, missing_error = inline_axioms.parse_inline_axiom_messages(
        [{"message": query.begin_marker}], query
    )
    duplicate, duplicate_error = inline_axioms.parse_inline_axiom_messages(
        [
            {"message": query.begin_marker},
            {"message": query.begin_marker},
            {"message": "'Demo.demo' does not depend on any axioms"},
            {"message": query.end_marker},
        ],
        query,
    )
    ambiguous, ambiguous_error = inline_axioms.parse_inline_axiom_messages(
        [
            {"message": query.begin_marker},
            {
                "message": (
                    "'Demo.demo' depends on axioms: [propext] and does not depend on any axioms"
                )
            },
            {"message": query.end_marker},
        ],
        query,
    )

    assert missing is None and "missing or ambiguous" in missing_error
    assert duplicate is None and "missing or ambiguous" in duplicate_error
    assert ambiguous is None and "missing or ambiguous" in ambiguous_error


def test_inline_axiom_query_rejects_multiline_target_identity():
    assert (
        inline_axioms.build_inline_axiom_query(
            "theorem demo : True := by trivial",
            target="demo\n#print axioms other",
            requested_target="demo",
        )
        is None
    )
