"""Record command provenance and expose expansion uncertainty to structural adapters."""

from __future__ import annotations

from typing import Any

from .source_provenance import COMMAND, identity
from .source_tex import TexBundle

_BUILTINS = set("""
begin end label ref eqref pageref cref Cref autoref cite citep citet nocite url href
chapter section subsection subsubsection paragraph title author date maketitle
tableofcontents bibliography bibliographystyle addbibresource printbibliography
documentclass usepackage RequirePackage ProvidesPackage ProvidesClass makeatletter
makeatother includeonly input include subfile includegraphics newpage clearpage
appendix pagestyle thispagestyle setcounter addtocounter newcounter item footnote
text textbf textit texttt textrm textsc textsf textnormal emph mbox hbox fbox par noindent indent quad qquad
smallskip medskip bigskip hfill vfill hspace vspace linebreak newline allowbreak
left right middle big Big bigg Bigg frac dfrac tfrac cfrac binom dbinom tbinom
sqrt sum prod coprod int iint iiint oint lim limsup liminf sup inf max min
sin cos tan cot sec csc sinh cosh tanh log ln exp det gcd dim deg ker hom Hom
mathbb mathcal mathfrak mathrm mathsf mathtt mathit mathbf mathnormal boldsymbol bm operatorname
overline underline widehat widetilde hat bar tilde vec dot ddot partial nabla
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa
lambda mu nu xi omicron pi varpi rho varrho sigma varsigma tau upsilon phi varphi
chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
forall exists nexists in notin ni subset subseteq supset supseteq nsubseteq
cup cap bigcup bigcap setminus times otimes oplus circ bullet cdot ast star
le leq ge geq ne neq equiv cong sim simeq approx propto ll gg prec succ preceq
succeq lesssim gtrsim coloneqq colon to mapsto rightarrow leftarrow leftrightarrow
Rightarrow Leftarrow Leftrightarrow implies iff longrightarrow longmapsto hookrightarrow
land lor wedge vee neg not lnot top bot perp models vdash dashv mid parallel
emptyset varnothing infty aleph hbar ell Re Im lvert rvert lVert rVert vert Vert
langle rangle lceil rceil lfloor rfloor langle rangle braces dots ldots cdots vdots ddots
substack underbrace overbrace underset overset limits nolimits displaystyle textstyle
scriptstyle scriptscriptstyle nonumber notag tag eqno qed qedhere hline cline multicolumn
centering caption captionof ensuremath newlength setlength addtolength fontsize selectfont
lean uses leanok mathlibok notready discussion proves inputencoding
proof endproof profess endprofess centerline bfseries itshape thanks today
hypersetup setlist theoremstyle twocolumn onecolumn linewidth textwidth FloatBarrier
impliedby Pr balance appendices begingroup endgroup npreceq mathds frown upharpoonright
downarrow sqsubseteq
""".split())


def command_references(bundle: TexBundle, expanded: str) -> list[dict[str, Any]]:
    """Identify local definitions and retain unresolved commands without expanding them."""
    definitions: dict[str, list[str]] = {}
    for scan in bundle.scans.values():
        for definition in scan.definitions:
            definitions.setdefault(definition["name"], []).append(definition["id"])
    references: list[dict[str, Any]] = []
    unknown: dict[str, list[dict[str, Any]]] = {}
    for command in COMMAND.finditer(expanded):
        name = command[1].rstrip("*")
        spans = bundle.spans(command.start(), command.end())
        known = name in _BUILTINS or not name.isalpha()
        resolution = (
            "local_definition"
            if name in definitions
            else "latex_builtin" if known else "unresolved"
        )
        reference = {
            "id": identity("command", bundle.document_id + ":" + str(command.start()) + ":" + name),
            "name": name,
            "spans": spans,
            "definition_refs": definitions.get(name, []),
            "resolution": resolution,
        }
        references.append(reference)
        if resolution == "unresolved":
            unknown.setdefault(name, []).extend(spans)
    for name, spans in unknown.items():
        bundle.diagnostic(
            "unresolved_command",
            f"Command \\{name} needs package, adapter, or manual semantic reconciliation",
            spans[0]["file"] if spans else "",
            spans=spans,
        )
    return references
