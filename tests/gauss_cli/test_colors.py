import gauss_cli.colors as colors


def test_render_terminal_text_strips_ansi_and_unicode_for_plain_terminals():
    rendered = colors.render_terminal_text(
        "\033[33m⚠ Run /autoprove — then attach\033[0m",
        allow_ansi=False,
        allow_unicode=False,
    )

    assert rendered == "! Run /autoprove - then attach"


def test_color_returns_plain_ascii_when_terminal_has_no_ansi(monkeypatch):
    monkeypatch.setattr(colors, "supports_ansi", lambda stream=None: False)
    monkeypatch.setattr(colors, "supports_unicode", lambda stream=None: False)

    rendered = colors.color("✓ Ready — now", colors.Colors.GREEN)

    assert rendered == "OK Ready - now"


def test_spinner_frames_fall_back_to_ascii(monkeypatch):
    monkeypatch.setattr(colors, "supports_unicode", lambda stream=None: False)

    assert colors.spinner_frames() == ("|", "/", "-", "\\")
