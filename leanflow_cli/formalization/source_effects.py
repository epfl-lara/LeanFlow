"""Classify recognized TeX file loading and I/O without executing source or packages."""

from __future__ import annotations

LITERAL_FILE_COMMANDS = frozenset(
    {
        "input",
        "include",
        "subfile",
        "usepackage",
        "RequirePackage",
        "documentclass",
        "includegraphics",
        "bibliography",
        "addbibresource",
    }
)
UNEXPANDED_FILE_COMMANDS = frozenset(
    {
        "@input",
        "@@input",
        "@input@",
        "import",
        "subimport",
        "Import",
        "SubImport",
        "inputfrom",
        "subinputfrom",
        "includefrom",
        "subincludefrom",
        "InputIfFileExists",
        "IfFileExists",
        "includepdf",
        "includestandalone",
    }
)
TEX_IO_EXECUTION_COMMANDS = UNEXPANDED_FILE_COMMANDS | frozenset(
    {
        "write",
        "read",
        "readline",
        "openin",
        "openout",
        "closein",
        "closeout",
        "directlua",
        "latelua",
        "luaexec",
        "luafunction",
        "luafunctioncall",
        "luadef",
        "special",
        "pdfextension",
        "pdfliteral",
        "pdfobj",
        "pdfximage",
        "pdffiledump",
        "pdffilemoddate",
        "pdffilesize",
        "XeTeXpicfile",
        "XeTeXpdffile",
        "shipout",
        "dump",
        "message",
        "errmessage",
        "show",
        "showthe",
        "showbox",
    }
)
DEFINITION_FILE_EFFECTS = TEX_IO_EXECUTION_COMMANDS | LITERAL_FILE_COMMANDS
