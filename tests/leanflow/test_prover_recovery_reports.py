"""Keep bounded checker evidence without interpreting failed refutations as truth."""

from leanflow_cli.workflows.prover.recovery_reports import MAX_DIAGNOSTICS, negation_report


def test_negation_report_retains_nested_errors_without_dumping_proofs() -> None:
    report = negation_report(
        {
            "certified": False,
            "verification": {
                "accepted": False,
                "inspect": {"error": "signature mismatch", "messages": [{"message": "detail"}]},
                "kernel_profile": {"error": "forbidden axiom", "output": "profile output"},
                "source": "a large proof that is not diagnostic feedback",
            },
        },
        {"found": True},
    )
    assert report["verification"]["diagnostics"].splitlines() == [
        "signature mismatch",
        "detail",
        "forbidden axiom",
        "profile output",
    ]
    assert report["screen"] == {"found": True}


def test_negation_report_bounds_diagnostics_and_handles_missing_verification() -> None:
    report = negation_report(
        {"verification": {"error": "x" * (MAX_DIAGNOSTICS + 100)}, "notes": "n" * 3000}, None
    )
    assert len(report["verification"]["diagnostics"]) == MAX_DIAGNOSTICS
    assert len(report["notes"]) == 2000
    assert negation_report({}, None) == {"notes": "", "screen": None, "certified": False}


def test_compiler_streams_survive_independently_of_long_model_notes() -> None:
    report = negation_report(
        {
            "notes": "n" * 3000,
            "verification": {
                "accepted": False,
                "compile": {"stderr": "error: expected tactic", "stdout": "source location"},
            },
        },
        None,
    )
    assert report["verification"]["diagnostics"] == "error: expected tactic\nsource location"
