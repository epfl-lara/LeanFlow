from unittest.mock import MagicMock

from cli import RuntimeConsole


def test_runtime_console_uses_rich_console_without_tui():
    console = RuntimeConsole(lambda: None)
    console._rich.print = MagicMock()
    console._chat.print = MagicMock()

    console.print("[bold]hello[/]")

    console._rich.print.assert_called_once_with("[bold]hello[/]")
    console._chat.print.assert_not_called()


def test_runtime_console_uses_chat_console_with_tui():
    console = RuntimeConsole(lambda: object())
    console._rich.print = MagicMock()
    console._chat.print = MagicMock()

    console.print("[bold]hello[/]")

    console._chat.print.assert_called_once_with("[bold]hello[/]")
    console._rich.print.assert_not_called()


def test_runtime_console_clear_uses_prompt_toolkit_output_when_tui_active():
    output = MagicMock()
    app = MagicMock(output=output)
    console = RuntimeConsole(lambda: app)
    console._rich.clear = MagicMock()

    console.clear()

    output.erase_screen.assert_called_once_with()
    output.cursor_goto.assert_called_once_with(0, 0)
    output.flush.assert_called_once_with()
    console._rich.clear.assert_not_called()


def test_runtime_console_width_uses_terminal_size_when_tui_active(monkeypatch):
    console = RuntimeConsole(lambda: object())
    monkeypatch.setattr("cli.shutil.get_terminal_size", lambda fallback=(80, 24): MagicMock(columns=123))

    assert console.width == 123
