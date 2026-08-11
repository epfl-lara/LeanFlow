from core.toolsets import (
    _COORDINATION_TOOLS,
    _DELEGATION_TOOLS,
    _DOCUMENT_TOOLS,
    _FILE_TOOLS,
    _LEANFLOW_CORE_TOOLS,
    _SESSION_TOOLS,
    _SKILL_TOOLS,
    _TERMINAL_TOOLS,
    _WEB_RESEARCH_TOOLS,
    _WEB_TOOLS,
    get_toolset_info,
    resolve_multiple_toolsets,
    resolve_toolset,
    validate_toolset,
)


def test_leanflow_native_contains_exactly_core_tools():
    tools = set(resolve_toolset("leanflow-native"))
    expected = set(_LEANFLOW_CORE_TOOLS)

    assert tools == expected, f"extra={tools - expected}, missing={expected - tools}"


def test_leanflow_native_has_no_delegation_tools():
    tools = set(resolve_toolset("leanflow-native"))
    for t in _DELEGATION_TOOLS:
        assert t not in tools, f"delegation tool {t!r} leaked into leanflow-native"


def test_leanflow_native_swarm_adds_delegate_task_to_core():
    native = set(resolve_toolset("leanflow-native"))
    swarm = set(resolve_toolset("leanflow-native-swarm"))

    assert swarm == native | set(
        _DELEGATION_TOOLS
    ), f"swarm should be native + delegation. extra={swarm - native - set(_DELEGATION_TOOLS)}"


def test_each_group_present_in_core_tools():
    core = set(_LEANFLOW_CORE_TOOLS)

    for tool in _FILE_TOOLS:
        assert tool in core, f"file tool {tool!r} missing from core"
    for tool in _WEB_TOOLS:
        assert tool in core, f"web tool {tool!r} missing from core"
    for tool in _TERMINAL_TOOLS:
        assert tool in core, f"terminal tool {tool!r} missing from core"
    for tool in _SKILL_TOOLS:
        assert tool in core, f"skill tool {tool!r} missing from core"
    for tool in _SESSION_TOOLS:
        assert tool in core, f"session tool {tool!r} missing from core"
    for tool in _COORDINATION_TOOLS:
        assert tool in core, f"coordination tool {tool!r} missing from core"
    for tool in _DOCUMENT_TOOLS:
        assert tool in core, f"document tool {tool!r} missing from core"
    assert "apply_verified_patch" in core
    assert "lean_reasoning_help" in core
    assert "lean_decompose_helpers" in core


def test_resolve_toolset_returns_empty_for_unknown_name():
    result = resolve_toolset("nonexistent-toolset-xyz")
    assert result == []


def test_lean_research_keeps_checks_without_shared_patch_authority():
    tools = set(resolve_toolset("lean-research"))

    assert "lean_incremental_check" in tools
    assert "lean_axioms" in tools
    assert "lean_proof_context" in tools
    assert "apply_verified_patch" not in tools
    assert "lean_reasoning_help" not in tools
    assert "lean_decompose_helpers" not in tools
    assert "write_file" not in tools
    assert "patch" not in tools


def test_web_research_has_no_project_state_writers():
    tools = set(resolve_toolset("web-research"))

    assert tools == set(_WEB_RESEARCH_TOOLS) == {"web_search", "web_fetch"}
    assert tools.isdisjoint({"web_download", "repo_clone"})


def test_scratch_research_union_resolves_to_read_check_only_tools():
    tools = set(resolve_multiple_toolsets(["web-research", "lean-research"]))

    assert {"web_search", "web_fetch", "lean_incremental_check"} <= tools
    assert tools.isdisjoint(
        {
            "web_download",
            "repo_clone",
            "apply_verified_patch",
            "lean_reasoning_help",
            "lean_decompose_helpers",
            "write_file",
            "patch",
        }
    )


def test_empirical_compute_is_not_part_of_general_or_research_toolsets():
    compute = set(resolve_toolset("empirical-compute"))

    assert compute == {"empirical_compute"}
    assert "empirical_compute" not in resolve_toolset("leanflow-native")
    assert "empirical_compute" not in resolve_toolset("lean-research")


def test_autoformalize_is_composite_and_includes_core_groups():
    tools = set(resolve_toolset("autoformalize"))

    for t in _FILE_TOOLS:
        assert t in tools, f"autoformalize missing file tool {t!r}"
    for t in _WEB_TOOLS:
        assert t in tools, f"autoformalize missing web tool {t!r}"
    for t in _TERMINAL_TOOLS:
        assert t in tools, f"autoformalize missing terminal tool {t!r}"
    for t in _COORDINATION_TOOLS:
        assert t in tools, f"autoformalize missing coordination tool {t!r}"
    for t in _DOCUMENT_TOOLS:
        assert t in tools, f"autoformalize missing document tool {t!r}"
    assert "apply_verified_patch" in tools


def test_resolved_toolsets_contain_no_duplicates():
    for name in (
        "leanflow-native",
        "leanflow-native-swarm",
        "leanflow-prove-worker",
        "lean-research",
        "web-research",
        "empirical-compute",
        "autoformalize",
        "leanflow-cli",
    ):
        tools = resolve_toolset(name)
        assert len(tools) == len(set(tools)), f"{name!r} has duplicate tools: {tools}"


def test_prove_worker_toolset_drops_session_and_document_noise():
    prove = set(resolve_toolset("leanflow-prove-worker"))
    native = set(resolve_toolset("leanflow-native"))

    # Inner proof worker drops cross-session recall and source-document inspection.
    for dropped in ("session_search", "formalization_document_inspect", "read_pdf"):
        assert dropped not in prove, f"{dropped!r} should not be in the prove-worker toolset"
        assert dropped in native, f"{dropped!r} should still be in leanflow-native"

    # All Lean tools and the core edit/read/search tools remain available (web kept too).
    for kept in (
        "lean_inspect",
        "lean_incremental_check",
        "lean_search",
        "lean_decompose_helpers",
        "lean_reasoning_help",
        "read_file",
        "patch",
        "apply_verified_patch",
        "web_search",
    ):
        assert kept in prove, f"{kept!r} should be in the prove-worker toolset"

    assert prove == native - {"session_search", "formalization_document_inspect", "read_pdf"}


def test_validate_toolset_accepts_known_names_and_wildcards():
    for name in (
        "leanflow-native",
        "leanflow-native-swarm",
        "autoformalize",
        "coordination",
        "document",
        "file",
        "terminal",
        "empirical-compute",
        "web-research",
    ):
        assert validate_toolset(name) is True, f"validate_toolset should accept {name!r}"

    assert validate_toolset("all") is True
    assert validate_toolset("*") is True


def test_validate_toolset_rejects_unknown_names():
    assert validate_toolset("imaginary-toolset") is False
    assert validate_toolset("") is False


def test_get_toolset_info_returns_correct_structure_for_native():
    info = get_toolset_info("leanflow-native")

    assert info is not None
    assert info["name"] == "leanflow-native"
    assert info["is_composite"] is False
    assert set(info["direct_tools"]) == set(_LEANFLOW_CORE_TOOLS)
    assert info["tool_count"] == len(set(_LEANFLOW_CORE_TOOLS))


def test_get_toolset_info_marks_autoformalize_as_composite():
    info = get_toolset_info("autoformalize")

    assert info is not None
    assert info["is_composite"] is True
    assert len(info["includes"]) > 0
    assert set(info["direct_tools"]) == set()


def test_get_toolset_info_returns_none_for_unknown():
    assert get_toolset_info("not-a-toolset") is None


def test_leanflow_cli_toolset_matches_native_tool_surface():
    cli_tools = set(resolve_toolset("leanflow-cli"))
    native_tools = set(resolve_toolset("leanflow-native"))

    assert cli_tools == native_tools, (
        f"cli and native should share the same tool surface. "
        f"extra in cli={cli_tools - native_tools}, missing from cli={native_tools - cli_tools}"
    )
