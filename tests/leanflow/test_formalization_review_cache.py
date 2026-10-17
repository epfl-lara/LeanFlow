"""Keep negative reviews stable under comments without discarding semantic inputs."""

from leanflow_cli.formalization.review_cache import _lean_evidence, rejection_key


def test_comment_width_is_ignored_but_indentation_and_literals_remain_bound():
    first = "theorem t : True := by /- short -/ sorry\n"
    second = "-- preface\ntheorem t : True := by /- much longer explanation -/ sorry\n"
    assert _lean_evidence(first) == _lean_evidence(second)
    assert _lean_evidence(first) == _lean_evidence("theorem t : True := by sorry\n")
    assert _lean_evidence(first) == _lean_evidence("/- prefix -/theorem t : True := by sorry\n")
    assert _lean_evidence("theorem t : True := by\n  trivial\n") != _lean_evidence(
        "theorem t : True := by\n    trivial\n"
    )
    assert _lean_evidence('def message := "a\n\nb"\n') != _lean_evidence('def message := "a\nb"\n')
    assert _lean_evidence('def message := "a  b"\n') != _lean_evidence('def message := "a b"\n')


def test_unsupported_context_syntax_uses_exact_bytes_without_crashing(monkeypatch):
    from leanflow_cli.formalization import review_cache

    documents = {
        "lean:lakefile.lean": {"text": 'package p where\n  moreLeanArgs := #[s!"value {1}"]'}
    }
    monkeypatch.setattr(review_cache, "evidence_documents", lambda record: documents)
    record = dict(
        inventory={},
        source_bundle={},
        environment_id="env",
        policy_id="policy",
        layout={},
        units=[],
        checks={"declarations": []},
    )
    dossier = {"phase": "translation", "units": [], "regions": []}
    first = rejection_key(record, dossier, {})
    documents["lean:lakefile.lean"]["text"] += "\n-- new context bytes"
    assert first != rejection_key(record, dossier, {})


def test_definition_elaboration_hash_is_retained_when_source_text_is_identical(monkeypatch):
    from leanflow_cli.formalization import review_cache

    monkeypatch.setattr(
        review_cache,
        "evidence_documents",
        lambda record: {"lean:Main.lean": {"text": "def line := __LINE__"}},
    )
    record = dict(
        inventory={},
        source_bundle={},
        environment_id="env",
        policy_id="policy",
        layout={},
        units=[],
        checks={"declarations": [{"name": "line", "kind": "definition", "body_hash": "line-one"}]},
    )
    dossier = {"phase": "translation", "units": [], "regions": []}
    first = rejection_key(record, dossier, {})
    record["checks"]["declarations"][0]["body_hash"] = "line-two"
    assert first != rejection_key(record, dossier, {})
