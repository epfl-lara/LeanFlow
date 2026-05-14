from toolsets import (
    _COORDINATION_TOOLS,
    _DELEGATION_TOOLS,
    _DOCUMENT_TOOLS,
    _EPFLEMMA_CORE_TOOLS,
    _FILE_TOOLS,
    _SESSION_TOOLS,
    _SKILL_TOOLS,
    _TERMINAL_TOOLS,
    _WEB_TOOLS,
    get_toolset_info,
    resolve_toolset,
    validate_toolset,
)


def test_epflemma_native_contains_exactly_core_tools():
    tools = set(resolve_toolset("epflemma-native"))
    expected = set(_EPFLEMMA_CORE_TOOLS)

    assert tools == expected, f"extra={tools - expected}, missing={expected - tools}"


def test_epflemma_native_has_no_delegation_tools():
    tools = set(resolve_toolset("epflemma-native"))
    for t in _DELEGATION_TOOLS:
        assert t not in tools, f"delegation tool {t!r} leaked into epflemma-native"


def test_epflemma_native_swarm_adds_delegate_task_to_core():
    native = set(resolve_toolset("epflemma-native"))
    swarm = set(resolve_toolset("epflemma-native-swarm"))

    assert swarm == native | set(_DELEGATION_TOOLS), (
        f"swarm should be native + delegation. extra={swarm - native - set(_DELEGATION_TOOLS)}"
    )


def test_each_group_present_in_core_tools():
    core = set(_EPFLEMMA_CORE_TOOLS)

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
    for name in ("epflemma-native", "epflemma-native-swarm", "autoformalize", "epflemma-cli"):
        tools = resolve_toolset(name)
        assert len(tools) == len(set(tools)), f"{name!r} has duplicate tools: {tools}"


def test_validate_toolset_accepts_known_names_and_wildcards():
    for name in ("epflemma-native", "epflemma-native-swarm", "autoformalize", "coordination", "document", "file", "terminal"):
        assert validate_toolset(name) is True, f"validate_toolset should accept {name!r}"

    assert validate_toolset("all") is True
    assert validate_toolset("*") is True


def test_validate_toolset_rejects_unknown_names():
    assert validate_toolset("imaginary-toolset") is False
    assert validate_toolset("") is False


def test_get_toolset_info_returns_correct_structure_for_native():
    info = get_toolset_info("epflemma-native")

    assert info is not None
    assert info["name"] == "epflemma-native"
    assert info["is_composite"] is False
    assert set(info["direct_tools"]) == set(_EPFLEMMA_CORE_TOOLS)
    assert info["tool_count"] == len(set(_EPFLEMMA_CORE_TOOLS))


def test_get_toolset_info_marks_autoformalize_as_composite():
    info = get_toolset_info("autoformalize")

    assert info is not None
    assert info["is_composite"] is True
    assert len(info["includes"]) > 0
    assert set(info["direct_tools"]) == set()


def test_get_toolset_info_returns_none_for_unknown():
    assert get_toolset_info("not-a-toolset") is None


def test_epflemma_cli_toolset_matches_native_tool_surface():
    cli_tools = set(resolve_toolset("epflemma-cli"))
    native_tools = set(resolve_toolset("epflemma-native"))

    assert cli_tools == native_tools, (
        f"cli and native should share the same tool surface. "
        f"extra in cli={cli_tools - native_tools}, missing from cli={native_tools - cli_tools}"
    )
