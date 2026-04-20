from toolsets import resolve_toolset


def test_epflemma_native_toolset_includes_kernel_tools():
    tools = resolve_toolset("epflemma-native")

    assert "terminal" in tools
    assert "read_file" in tools
    assert "web_search" in tools
    assert "session_search" in tools
    assert "delegate_task" not in tools
    assert "acquire_file_lock" in tools


def test_epflemma_native_swarm_toolset_includes_delegate_task():
    tools = resolve_toolset("epflemma-native-swarm")

    assert "delegate_task" in tools
    assert "acquire_file_lock" in tools
