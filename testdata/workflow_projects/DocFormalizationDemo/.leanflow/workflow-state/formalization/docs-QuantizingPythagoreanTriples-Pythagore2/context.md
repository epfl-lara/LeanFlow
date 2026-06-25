# EPFLemma Document Formalization Context

Source document: `docs/QuantizingPythagoreanTriples/Pythagore2.tex`
Input request: `docs/QuantizingPythagoreanTriples` (directory)
Document kind: `latex`
Detected title: Quantizing Pythagorean triples
Target Lean file: `DocFormalizationDemo/Pythagore2/Main.lean`
Planner blueprint: `/Users/lmilikic/Desktop/runai_start/vllm012/EPFLemma/testdata/workflow_projects/DocFormalizationDemo/DocFormalizationDemo/Pythagore2/Blueprint.md`
Extracted text cache: `/Users/lmilikic/Desktop/runai_start/vllm012/EPFLemma/testdata/workflow_projects/DocFormalizationDemo/.epflemma/workflow-state/formalization/docs-QuantizingPythagoreanTriples-Pythagore2/extracted.txt`
Preflight manifest: `/Users/lmilikic/Desktop/runai_start/vllm012/EPFLemma/testdata/workflow_projects/DocFormalizationDemo/.epflemma/workflow-state/formalization/docs-QuantizingPythagoreanTriples-Pythagore2/manifest.json`

## Hard Workflow Contract

This is a document formalization run. Do not treat the request as a proof-only repair.

Required planner phase:
1. Read the source document, nearby PDFs/figures/support files from this manifest, and this preflight manifest before drafting Lean.
2. Use `read_pdf` to read project-local PDF text, and use `formalization_document_inspect` for deterministic re-inspection when the source is a .tex or .pdf file.
3. Search local project facts and Mathlib before inventing names or definitions.
4. Use web search only for references or surrounding literature that the source document actually points to.
5. Create or update the planner blueprint before drafting Lean, recording definitions, lemmas, theorem dependencies, source pointers, formal-statement review, complete source proof text when available, and natural-language proof/prover notes. The initial `_pending_` blueprint is only a placeholder and does not satisfy the workflow.
6. Draft Lean files in small units with stable names, minimal imports, and `sorry` placeholders only for theorem/lemma/example proofs that a later explicit prove workflow should solve. Do not do deep proof repair in the planner draft.
7. Lean import discipline is mandatory: every generated Lean file must begin with all `import` commands before any `/-! ... -/` module doc comment or declaration.
8. Before marking the formalization proof-ready, satisfy the document formalization handoff verifier: keep `## Import Plan` to direct Lean imports only, put non-gating search hints under `## Suggested Search Modules`, ensure the root project module imports the generated target module path so plain `lake build` covers it, and keep direct imports aligned with the target Lean files.
9. Verify draft readiness with `lean_inspect`, then `lean_verify(mode=project)` after the generated files and root imports are in place. Module/file checks are useful while iterating, but they do not satisfy the final formalization gate.
10. Stop after the source map, blueprint, theorem statements, and proof `sorry` skeletons are ready. Ask for an independent statement/source verification pass; verifier agents are read-only reviewers and drafting agents apply corrections.
11. Definition, structure, class, and instance construction gaps block proof handoff. A declaration such as `noncomputable def foo := sorry` is not a theorem queue item; either implement the construction or record the blocker instead of launching `/prove`.
12. Use a multi-file generated layout when planned declarations exceed 12, proof obligations exceed 8, or source sections naturally separate definitions/constructions/main results. Prefer a module directory with `Main.lean` as aggregator and split files such as `Basic.lean`, `Constructions.lean`, and `Theorems.lean` when that improves import order.
13. Do not check the proof-ready checklist item yourself. Leave it unchecked until independent statement/source review has approved every source entry.

Statement fidelity:
- keep source pointers, ambiguity notes, dependencies, complete source proof text, and proof notes in the planner blueprint
- put a compact `Source proof` / `Proof sketch` / `Prover notes` paragraph in the Lean doc comment immediately above each source theorem or lemma so the prover gets the right proof nudge immediately
- the generated supplemental blueprint skill keeps the `Blueprint.md` path available to prover turns after compaction
- explicitly compare each Lean statement against the corresponding source statement before marking the formalization proof-ready
- when the source theorem quantifies over a structured object class or representation, do not count a simpler Lean encoding as full coverage unless a definition or companion declaration records the bridge
- if a representation bridge is intentionally omitted, mark the Lean coverage as partial and record the representation change under `Scope changes`; do not approve the entry as exact source coverage
- record `Statement verification status: approved` only after the verification pass has checked source-proof completeness, doc-comment nudges, and Lean statement correctness
- do not silently weaken or strengthen the source theorem
- avoid adding Lean comments unless they clarify a concrete formalization choice
- the blueprint is intentionally next to the Lean files so planner and prover turns can reread it easily

Proof phase:
- After the declaration skeleton is stable and statement/source verification is approved, the formalizer exits. Do not start the prover queue or a fresh prove workflow automatically.
- Print/log the suggested `/prove` command so the user can review the generated formalization before starting proof search explicitly.
- The theorem queue includes only `theorem`, `lemma`, and `example` proof obligations; construction stubs block handoff.
- Do not force suggested search modules into `.lean` imports. The prover may add imports when needed, then update the direct import plan.
- If the handoff verifier blocks the queue, update the root module, target imports, or blueprint first; do not work around the blocker by editing theorem statements opportunistically.
- When proving, consult the nearby blueprint and the original source document for natural-language proof strategy before inventing a proof.
- Keep blueprint entries aligned when a theorem is split or renamed.
- Completion still requires clean diagnostics, no open goals, no `sorry` in the requested scope, and final Lean verification.

Blueprint format:
- The default artifact is Markdown so it works without extra dependencies.
- If the project already has a `blueprint/` directory or `leanblueprint` is available, also keep a leanblueprint-compatible TeX blueprint in sync using `\lean`, `\uses`, and `\leanok` when appropriate.

## Detected Sections

- line 256: The $q$-deformed Pythagoras equation
- line 329: An interesting class of solutions
- line 443: Classical Pythagorean triples
- line 452: $\SL(2,\Z)$-action
- line 501: The Pythagorean tree
- line 585: Euclide's formula in a matrix form
- line 642: Relation to continued fractions
- line 679: A brief account on $q$-rationals
- line 688: An intrinsic definition
- line 719: The $q$-deformed $\PSL(2,\Z)$-action
- line 744: An explicit formula
- line 777: The total positivity property
- line 797: The inverse $q$-rational
- line 841: A construction of $q$-Pythagorean triples
- line 852: The main definition
- line 961: Solutions to the $q$-Pythagoras equation
- line 1102: Further examples

## Detected Theorem-Like Blocks

1. `[unlabeled]` (defn, lines 948-953)
   Source statement: For every rational $\frac{m}{n}$, we thus naturally associate the polynomial $$ \cC_{\frac{m}{n}}(q):=q\cN_{\frac{m}{n}}(q)^2+\cD_{\frac{m}{n}}(q)^2. $$

## TeX Project Discovery

Selected `docs/QuantizingPythagoreanTriples/Pythagore2.tex` from `docs/QuantizingPythagoreanTriples`; 0 included .tex file(s), 0 bibliography file(s), 7 referenced local asset file(s), 1 PDF file(s), 1 figure file(s), 0 TeX support file(s).

Included TeX files:
- [none]

Bibliography files:
- [none]

Local assets:
- `docs/QuantizingPythagoreanTriples/00README.json`
- `docs/QuantizingPythagoreanTriples/Pythagore2.aux`
- `docs/QuantizingPythagoreanTriples/Pythagore2.fdb_latexmk`
- `docs/QuantizingPythagoreanTriples/Pythagore2.fls`
- `docs/QuantizingPythagoreanTriples/Pythagore2.log`
- `docs/QuantizingPythagoreanTriples/Pythagore2.out`
- `docs/QuantizingPythagoreanTriples/Pythagore2.synctex.gz`

Nearby PDF files:
- `docs/QuantizingPythagoreanTriples/Pythagore2.pdf`

Figure/image files:
- `docs/QuantizingPythagoreanTriples/Pythagore2.pdf`

TeX support files:
- [none]

## Source Excerpt

```text
\documentclass{article}[12pt]
\usepackage[utf8]{inputenc}
\usepackage{authblk}
\usepackage{amsmath, amssymb, amsthm, amsfonts, gensymb, tikz, commath, float, mathtools, enumerate, breqn, circuitikz}
\usetikzlibrary{positioning, arrows, arrows.meta, cd}
\usetikzlibrary{shapes,backgrounds,positioning,petri,topaths,calc}

\usepackage[all]{xy}
\usepackage{tabularx}
\usepackage{multirow}
\usepackage{esvect}
\usepackage{subcaption}
\usepackage{stmaryrd} % For Hirzebruch continued fractions notation

\usepackage[text={15cm,22cm}]{geometry} % Imposta pagina
%\usepackage{hyperref}
\usepackage[colorlinks=true,%
            linkcolor=red!50!black,%
            citecolor=blue!50!black,%
            urlcolor=darkgray]{hyperref}  % Needs to go last
% 
% ----------------------------------------------------------------
\vfuzz2pt % Don't report over-full v-boxes if over-edge is small
\hfuzz2pt % Don't report over-full h-boxes if over-edge is small

\setlength{\textwidth}{16truecm}
%\setlength{\textheight}{21truecm}
\setlength{\hoffset}{-0.5truecm}
%\setlength{\voffset}{-1.5truecm}

% THEOREMS -------------------------------------------------------


\newtheorem{mainthm}{Theorem}
\newtheorem{maincor}{Corollary}
\renewcommand{\themainthm}{\Alph{mainthm}}
\renewcommand{\themaincor}{\Alph{maincor}}

\theoremstyle{plain}
\newtheorem{fac}{Fact}
\newtheorem{com}{Comment}%[section]
\newtheorem{lem}{Lemma}[section]
\newtheorem{thm}{Theorem}
\newtheorem*{thmE}{Euclide's Theorem}
\newtheorem{cor}[lem]{Corollary}
\newtheorem{prop}[lem]{Proposition}
\newtheorem*{conj}{Conjecture}
\newtheorem{theo}{Theorem}

\theoremstyle{definition}
\newtheorem*{rem}{Remark}
\newtheorem{ex}[lem]{Example}
\newtheorem{exe}{Exercise}
\newtheorem{nota}[lem]{Notation}
\newtheorem{defn}[lem]{Definition}


%Caracteres MATH -----------------------------------------------------------

%raccourci
\newcommand{\qth}{q^{-1}[3]_q}

%BB
\newcommand{\Ab}{\mathbb{A}}
\newcommand{\R}{\mathbb{R}}
\newcommand{\Z}{\mathbb{Z}}
\newcommand{\C}{\mathbb{C}}
\newcommand{\N}{\mathbb{N}}
\newcommand{\bF}{\mathbb{F}}
\newcommand{\T}{\mathbb{T}}
\newcommand{\bP}{\mathbb{P}}
\newcommand{\Q}{\mathbb{Q}}
\newcommand{\K}{\mathbb{K}}
\newcommand{\RP}{{\mathbb{RP}}}
\newcommand{\pP}{{\mathbb{P}}}
\newcommand{\CP}{{\mathbb{CP}}}

%Bold
\newcommand{\bc}{\mathbf{c}}
\newcommand{\bd}{\mathbf{d}}
\newcommand{\be}{\mathbf{e}}
\newcommand{\bx}{\mathbf{x}}
\newcommand{\ev}{\mathbf{ev}}
\newcommand{\bev}{\overline{\mathbf{ev}}}

%Caligraphie
\newcommand{\A}{\mathcal{A}}
\newcommand{\B}{\mathcal{B}}
\newcommand{\cC}{\mathcal{C}}
\newcommand{\cD}{\mathcal{D}}
\newcommand{\X}{\mathcal{X}}
\newcommand{\F}{\mathcal{F}}
\newcommand{\G}{\mathcal{G}}
\newcommand{\cK}{\mathcal{K}}
\newcommand{\cL}{\mathcal{L}}
\newcommand{\cN}{\mathcal{N}}
\newcommand{\cM}{\mathcal{M}}
\newcommand{\cR}{\mathcal{R}}
\newcommand{\Qc}{\mathcal{Q}} 
\newcommand{\Pc}{\mathcal{P}}
\newcommand{\Sc}{\mathcal{S}}
\newcommand{\W}{\mathcal{W}}
\newcommand{\cU}{\mathcal{U}}
\newcommand{\cV}{\mathcal{V}}

%gothic
\newcommand{\gn}{\mathfrak{n}}
\newcommand{\gog}{\mathfrak{g}}
\newcommand{\gu}{\mathfrak{u}}
\newcommand{\gb}{\mathfrak{b}}
\newcommand{\gh}{\mathfrak{h}}
\newcommand{\gp}{\mathfrak{p}}
\newcommand{\ga}{\mathfrak{a}}
\newcommand{\gT}{\mathfrak{T}}

%droit
\newcommand{\ii}{\textup{\bf{i}}}
\newcommand{\id}{\textup{Id}}
\newcommand{\Gr}{\textup{Gr}}
\newcommand{\Tr}{\textup{Tr}}
\newcommand{\gr}{\textup{gr}}
\newcommand{\ir}{\textup{Irr}}
\newcommand{\ih}{\textup{IH}}
\newcommand{\val}{\textup{val}}
\newcommand{\End}{\textup{End}}
\newcommand{\Hom}{\textup{Hom}}
\newcommand{\pf}{\mathrm{pf}}
\newcommand{\Id}{\mathrm{Id}}
\newcommand{\SL}{\mathrm{SL}}
\newcommand{\PSL}{\mathrm{PSL}}
\newcommand{\PGL}{\mathrm{PGL}}
\newcommand{\Span}{\mathrm{Span}}

\newcommand{\half}{\frac{1}{2}}
\newcommand{\thalf}{\frac{3}{2}}

%Grec
\def\a{\alpha}
\def\b{\beta}
\def\d{\delta}
\def\D{\Delta}
\def\Db{\overline{\Delta}}
\def\e{\varepsilon}
\def\g{\gamma}
\def\L{\Lambda}
\def\om{\omega}
\def\t{\tau}
\def\vfi{\varphi}
\def\vr{\varrho}
\def\l{\lambda}
\newcommand{\cc}{\Gamma}

%---------------------------------------------


\def\GCD{\mathop{\rm GCD}\nolimits}
\def\ndup{\mathop{\rm nd}\nolimits}
\def\pD{D[s]}
\def\pcD{\cD[s]}
\def\Res{\mathop{\rm Res}\nolimits}
\def\sgn{\mathop{\rm sgn}\nolimits}
\def\stup{\mathop{\rm st}\nolimits}
\def\thup{\mathop{\rm th}\nolimits}
\def\vecm{\bar{m}}
\def\vecv{\bar{v}}
\def\vecw{\bar{w}}


%---------------------------------------------------
\newcommand{\perrine}[1]{\textcolor{blue}{[Perrine: #1]}}
\newcommand{\sophie}[1]{\textcolor{red}{[Sophie: #1]}}
\newcommand{\valentin}[1]{\textcolor{magenta}{[Valentin: #1]}}
\newcommand{\sam}[1]{\textcolor{orange}{[Sam: #1]}}

\title{Quantizing Pythagorean triples}

\author{
Hugo Mathevet,
Sophie Morier-Genoud, %\and
Valentin Ovsienko}


\date{}

\begin{document}

\maketitle

A classical Pythagorean triple $(a,b,c)$ is a triplet
of positive integers $a,b$ and $c$ satisfying the Diophantine equation 
$$
a^2+b^2=c^2
$$
called the Pythagoras equation.
This antique subject has always been and remains an active field of research.
For a detailed account of its historical development,
the reader is invited to consult
Sierpi\'nski's classical book~\cite{Sie}.
A surprising and curious idea of developing the entire number theory through the 
Pythagoras prism is proposed in~\cite{Tak}.

A Pythagorean triple $(a,b,c)$ is called {\it primitive} if $a,b,c$ are coprime,
but we will also be interested in the case where the greater common divisor
$\gcd(a,b,c)$ of $a,b,c$ equals $2$.
We have two possibilities
$$
\gcd(a,b,c)=
\left\{
\begin{array}{l}
1,\\
2.
\end{array}
\right.
$$
When $\gcd(a,b,c)=1$, we will require that $a$ is even, 
when $\gcd(a,b,c)=2$, we will require that $a/2$ is odd.
Such Pythagorean triple $(a,b,c)$ is sometimes called {\it standard}; see, e.g.~\cite{Tra}.

The Euclide formula provides a simple way to associate a Pythagorean triple
with an arbitrary rational number~$\frac{m}{n}\geq1$.
Consider positive coprime integers $m\geq n$, then it is easy to check that
\begin{equation}
\label{EuclForm}
a=2mn,
\qquad
b=m^2-n^2,
\qquad
c=m^2+n^2
\end{equation}
form a Pythagorean triple.
To some extent, the following statement can be attributed to Euclide.

\begin{thmE}
\label{ClassThm}
For every standard Pythagorean triple
there exist coprime positive integers $m,n$ such that 
the triplet $(a,b,c)$ are given by~\eqref{EuclForm}.
\end{thmE}

In other words, standard Pythagorean triples are parametrized by rationals~$\frac{m}{n}>1$.
For instance, 
the first nontrivial Pythagorean triple $(4,3,5)$ corresponds to $\frac{2}{1}$,
the next standard (but not primitive!) triple $(6,8,10)$ corresponds to $\frac{3}{1}$,
then  $\frac{3}{2}$ produces $(12,5,13)$, etc.


The Pythagorean triple~\eqref{EuclForm} is primitive if and only if
$m$ and $n$ are coprime and not both odd.
For the  standard triples, this restriction is removed, that is,
$m$ and $n$ are arbitrary positive coprime integers.
This is an important reason to extend considerations from primitive to standard triples.


%%%%%%%%%%%%%%%%%%%%
%%%%%%%%%%%%%%%%%%%%
\section{The $q$-deformed Pythagoras equation}
%%%%%%%%%%%%%%%%%%%%
%%%%%%%%%%%%%%%%%%%%


The goal of this article is to introduce and study 
a new natural $q$-analogue of the Pythagoras equation.
We consider three polynomials,
$\A,\B,\cC$, in one variable denoted by~$q$ (following a certain tradition).
We say that $\A,\B,\cC$ satisfy the $q$-deformed Pythagoras equation if
\begin{equation}
\label{PythEq}
\A(q)^2+q\B(q)^2=\cC(q)\cC^*(q),
\end{equation}
where $\cC^*$ is the polynomial reciprocal to $\cC$, i.e.
\begin{equation}
\label{RecipEq}
\cC^*(q)=q^{\deg(\cC)}\cC(q^{-1}).
\end{equation}

To give an idea about the polynomials appearing
in our context, consider two elementary examples.
More examples will be given later.

(1)
Our first nontrivial example of solution to~\eqref{PythEq} is
the only solution corresponding to the first nontrivial primitive Pythagorean triple $(a,b,c)=(4,3,5)$
and satisfying the conditions \ref{Con1}--\ref{Con3}:
$$
\A(q)=1+q+q^2+q^3=:\left[4\right]_q,
\qquad\qquad
\B(q)=1+q+q^2=:\left[3\right]_q,
$$
with $\cC$ and $\cC^*$ given by
$$
\cC(q)=1+2q+q^2+q^3
\qquad\quad\hbox{and}\quad\qquad
\cC^*(q)=1+q+2q^2+q^3.
$$
Note that $\cC$ and $\cC^*$ are interchangeable and cannot be distinguished from each other.
Note also that the order matters: $a=4$, and $b=3$, but not vice-versa.
To better understand this example, recall that the polynomial
\begin{equation}
\label{qBn}
\left[n\right]_{q}:=1+q+q^{2}+\cdots+q^{n-1}=\textstyle\frac{1-q^n}{1-q},
\end{equation}
is commonly considered as the $q$-analogue of a (positive) integer~$n$.
Extensively used in such areas as quantum groups, quantum calculus, etc. this
notion goes back to Euler ($\approx$1760) and Gauss ($\approx$1808).

(2)
Our second example is obtained by doubling the previous one,
but the roles of $a$ and $b$ are exchanged: $(a,b,c)=(6,8,10)$.
Our solution to~\eqref{PythEq} in this case is
\begin{eqnarray*}
\A(q) &=& 1+q+q^2+q^3+q^4+q^5=\left[6\right]_q,
\\[4pt]
\B(q) &=& 1+2q+2q^2+2q^3+q^4=(1+q)(1+q^2)^2,
\\[4pt]
\cC(q) &=& 1+q+2q^2+3q^3+2q^4+q^5=(1+2q^2+q^3+q^4)(1+q).
\end{eqnarray*}

We did not find the equation~\eqref{PythEq} in the literature.
Let us mention that more straightforward polynomial generalizations of the Pythagoras equation
such as $\A(q)^2+\B(q)^2=\cC(q)^2$ have been considered by many authors; 
see, e.g.~\cite{DLS,CC}.
This equation is distantly related to the vast subject of sum of squares of polynomials
and Hilbert's seventeenth problem.
However, polynomials satisfying this equation cannot have nice properties 
we will be interested in.

%%%%%%%%%%%%%%%%%%%%
%%%%%%%%%%%%%%%%%%%%
\section{An interesting class of solutions}
%%%%%%%%%%%%%%%%%%%%
%%%%%%%%%%%%%%%%%%%%

We will search for solutions to the equation~\eqref{PythEq}
satisfying the following properties 
which seem quite natural from a combinatorial point of view.

\begin{enumerate}
\item
\label{Con1}
The polynomials $\A,\B,\cC$ have positive integer coefficients;

\item
\label{Con2}
The polynomials $\A$ and $\B$ are self-reciprocal, or ``palindromic'';

\item
\label{Con3}
The polynomials $\A,\B,\cC$ and $\cC^*$ are monic, 
i.e. their leading and lower degree coefficients are equal to~$1$.

\end{enumerate}

The positivity condition \ref{Con1} implies that the triple of integers 
$$
(a,b,c):=(\A(1),\B(1),\cC(1))
$$ 
is a classical Pythagorean triple.
We will therefore say that the triplet of polynomials $(\A,\B,\cC)$
corresponds to this Pythagorean triple
and is its $q$-analogue, or ``quantization''.
This term and notion is extensively used in mathematical physics, but also in combinatorics.
Roughly speaking, quantization means replacing a single quantity by a discrete
(finite) sequence that potentially contain more information.
In our situation, we replace single integers $a,b,c$ by sequences of coefficients
of the polynomials $\A,\B,\cC$, 
and each coefficient of these polynomials must have a meaning.
Another approach to quantization consists in replacing 
commutative algebraic structure by non-commutative, as explored in~\cite{AE}.
Note that two approaches are related, but their comparison is far beyond the scope of this article.

Our main result is the following existence statement.

\begin{thm}
\label{ExtUniq}
For every standard Pythagorean triple $(a,b,c)$
there exists a solution $(\A,\B,\cC)$ to~\eqref{PythEq}
satisfying the conditions \ref{Con1}, \ref{Con2}, and \ref{Con3}
and corresponding to $(a,b,c)$.
\end{thm}

To illustrate this theorem, let us give an infinite
  series of solutions enumerated by integers:
\begin{eqnarray*}
\A_{\frac{n}{1}}(q)&=&
(1+q^n)\left[n\right]_q,
\\[4pt]
\B_{\frac{n}{1}}(q)&=&
\left[n+1\right]_q\left[n-1\right]_q,
\\[4pt]
\cC_{\frac{n}{1}}(q)&=&
1+q\left[n\right]_q^2,
\end{eqnarray*}
where $\left[n\right]_q$ is the $q$-integer; see~\eqref{qBn}.

Our construction of polynomial Pythagorean triples is based on
the $q$-deformed action of the modular group $\PSL(2,\Z)$ on the rational projective line.
It was used in~\cite{MGOfmsigma} to define the notion of $q$-deformed rational numbers.
For more details, see~\cite{LMGadv} and  a survey~\cite{MGOmn}.
Note that our approach is quite close to that of~\cite{EJMGO}
where $q$-deformations of Markov triples were studied with the help of
the $q$-deformed action of the modular group.

Similarly to the classical case,
we obtain an infinite series of $q$-deformed Pythagorean triples
organized in a form of a binary tree.
Replacing the rational numbers with
$q$-rationals, we obtain a $q$-analogue of the Euclid formula.

We conjecture that our solutions have another remarkable property.
A sequence of real numbers is said to be {\it unimodal}
if it increases (not strictly monotonically) to a maximum, then decreases monotonically.
Unimodal sequences have a single peak and no oscillations.

We wish the following additional property
\begin{enumerate}
\item[$4^*$.]
\label{Con4}
The  sequences of coefficients of the polynomials $\A,\B,\cC$ are unimodal,
\end{enumerate}
but we are unable to guarantee it!
The unimodality property is usually difficult to prove.
Note that this property for $q$-deformed rational numbers
was conjectured in~\cite{MGOfmsigma} and proved in~\cite{OgRa}.


\begin{conj}
The  sequences of coefficients of the polynomials $\A,\B,\cC$
constructed in this article are unimodal.
\end{conj}


Let us mention that the solutions to~\eqref{PythEq} that we construct are
far from being the only existing solutions.
We will show that there are solutions to~\eqref{PythEq} 
satisfying the conditions \ref{Con1}, \ref{Con2}, \ref{Con3}, and the unimodality property
different from ours.
Their classification is a challenging problem.
It would also be interesting to understand what distinguishes 
the class of solutions related to $q$-rationals.

%%%%%%%%%%%%%%%%%%%%
%%%%%%%%%%%%%%%%%%%%
\section{Classical Pythagorean triples}
%%%%%%%%%%%%%%%%%%%%
%%%%%%%%%%%%%%%%%%%%

In this Section, we collect some simple and well-known facts about 
classical Pythagorean triples.
We attach great importance to the action of the modular group.

%%%%%%%%%%%%%%%%%%%%
\subsection{$\SL(2,\Z)$-action}
%%%%%%%%%%%%%%%%%%%%

Every Pythagorean triple $(a,b,c)$ can be identified with a symmetric
$2\times2$ matrix
\begin{equation}
\label{PythaM}
X_{(a,b,c)}=
\begin{pmatrix}
\frac{c+b}{2}&\frac{a}{2}\\[4pt]
\frac{a}{2}&\frac{c-b}{2}
\end{pmatrix}
\end{equation}
of rank~$1$, i.e. $\det(X_{(a,b,c)})=0$.
This is of course equivalent to the Pythagoras equation.
Moreover, the matrix $X_{(a,b,c)}$ has integer coefficients
if and only if $(a,b,c)$ is an integer multiple of a standard triple.
It is important to notice that $c$ is recovered from the above matrix as
the trace:
$$
c=\Tr(X_{(a,b,c)}).
$$
Clearly, $a$ and $b$ are also encoded by the matrix $X_{(a,b,c)}$,
but the trace is more fundamental and has an intrinsic meaning.

The interpretation of Pythagorean triples in the form of a matrix~\eqref{PythaM} 
allows one to define a natural action of
the group $\SL(2,\Z)$ of $2\times2$ unimodular matrices
$$
A=\begin{pmatrix}
\a&\b\\%[2pt]
\g&\d
\end{pmatrix},
\qquad\qquad
\a,\b,\g,\d\in\Z,
\quad
\a\d-\b\g=1
$$
on Pythagorean triples via
\begin{equation}
\label{LFAct}
X_{A(a,b,c)}:=
A\,X_{(a,b,c)}\,A^T,
\end{equation}
where $A^T$ is the matrix transposed to $A$.

The action~\eqref{LFAct} is transitive on the set of standard triples; see~\cite{Tra}.

%%%%%%%%%%%%%%%%%%%%
\subsection{The Pythagorean tree}
%%%%%%%%%%%%%%%%%%%%

The $\SL(2,\Z)$-action allows one to present the whole set of standard Pythagorean triples
in a form of a tree:
$$
\begin{small}
\xymatrix @!0 @R=0.48cm @C=0.48cm
{
&&&&&&&&&&&&&&&&(0,-1,1)\ar@{-}[dd]&&\\
&&&&&&&&&&&&&&&&\\
&&&&&&&&&&&&&&&&(2,0,2)\ar@{-}[dd]&&\\
&&&&&&&&&&&&&&&&\\
&&&&&&&&&&&&&&&&(4,3,5)\ar@{-}[lllllllldd]\ar@{-}[rrrrrrrrdd]&&&&&&&&\\
&&&&&&&&&&&&&&&&\\
&&&&&&&&(12,5,13)\ar@{-}[lllldd]\ar@{-}[rrrrdd]
&&&&&&&&&&&&&&&&(6,8,10)\ar@{-}[lllldd]\ar@{-}[rrrrdd]&&&\\
&&&&&&&&&&&&&&&&&&&&&&&&\\
&&&&(20,21,29)\ar@{-}[lldd]\ar@{-}[rrdd]
&&&&&&&&(30,16,34)\ar@{-}[lldd]\ar@{-}[rrdd]
&&&&&&&&(24,7,25)\ar@{-}[lldd]\ar@{-}[rrdd]
&&&&&&&&(8,15,17)\ar@{-}[lldd]\ar@{-}[rrdd]\\
&&&&&&&&&&&&&&&&&&&&&&&&&&&&\\
&&(28,45,53)\ar@{-}[ldd]\ar@{-}[rdd]
&&&&(70,24,74)\ar@{-}[ldd]\ar@{-}[rdd]
&&&&(80,39,89)\ar@{-}[ldd]\ar@{-}[rdd]
&&&&(48,55,73)\ar@{-}[ldd]\ar@{-}[rdd]
&&&&(42,40,58)\ar@{-}[ldd]\ar@{-}[rdd]
&&&&(56,33,65)\ar@{-}[ldd]\ar@{-}[rdd]
&&&&(40,9,41)\ar@{-}[ldd]\ar@{-}[rdd]
&&&&(10,24,26)\ar@{-}[ldd]\ar@{-}[rdd]\\
&&&&&&&&&&&&&&&&&&&&&&
&&&&&&&&&&&\\
%&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&\\
&&
&&&&
&&&&
&&&&
&&&&
&&&&
&&&&
&&&&&&\\
&&&&\ldots&&&&&&&&&&&&\ldots&&&&&&&&&&&&\ldots
}
\end{small}$$
Every right (resp. left) branch of the tree is obtained by the action~\eqref{LFAct} of the
standard generator~$R$ (resp.~$L$)
of $\SL(2,\Z)$, where
$$
R=
\begin{pmatrix}
1&1\\
0&1
\end{pmatrix},
\qquad\qquad
L=
\begin{pmatrix}
1&0\\
1&1
\end{pmatrix}.
$$
It is convenient to start the tree from the degenerate triple $(0,-1,1)$ corresponding to the matrix
\begin{equation}
\label{X0}
X_0=
\begin{pmatrix}
0&0\\%[2pt]
0&1
\end{pmatrix}.
\end{equation}
Consecutive actions of $R$ and $L$ on $(0,-1,1)$ 
lead to the triple $(4,3,5)$, and this is why the first two branches are presented as a vertical stem.


\begin{rem}
Another, perhaps more common, version of the Pythagorean tree that
contains only primitive Pythagorean triples can be obtained using the action
of the congruence subgroup $\Gamma(2)\subset\SL(2,\Z)$.
Our preference however is to keep the whole group $\SL(2,\Z)$,
which results in adding Pythagorean triples with $\gcd(a,b,c)=2$.
\end{rem}


%%%%%%%%%%%%%%%%%%%%
\subsection{Euclide's formula in a matrix form}
%%%%%%%%%%%%%%%%%%%%


In terms of the symmetric matrices~\eqref{PythaM}, the Euclide formula reads
$$
X_{(a,b,c)}=
\begin{pmatrix}
m^2&mn\\
mn&n^2
\end{pmatrix}=
\begin{pmatrix}
m\\
n
\end{pmatrix}
\begin{pmatrix}m, &n\end{pmatrix}.
$$
That is why it is practical to use another notation for $X_{(a,b,c)}$, namely
$X_{\frac{m}{n}}$.
The $\SL(2,\Z)$-action on pairs $(m,n)$ is then simply the linear action on $2$-vectors
$$
A:\begin{pmatrix}
m\\
n
\end{pmatrix}
\mapsto
A\begin{pmatrix}
m\\
n
\end{pmatrix},
$$
while the action on rational numbers is given by fractional-linear transformations
\begin{equation}
\label{LFT}
\begin{pmatrix}
\a&\b\\%[2pt]
\g&\d
\end{pmatrix}\left(x\right)=
\frac{\a x+\b}{\g x+\d}.
\end{equation}

Note that for $x\in\Q$ the result of~\eqref{LFT} can become infinite.
This is why the action~\eqref{LFT} is correctly understood as an action
on the rational projective line $\Q\cup\{\infty\}$,
where $\infty$ is represented by the quotient~$\frac{1}{0}$.
Note also that the center of  $\SL(2,\Z)$ acts trivially, therefore
is is more convenient to think of  $\PSL(2,\Z)$-action,
where $\PSL(2,\Z)$ is the quotient of $\SL(2,\Z)$ by the center: 
$$
\PSL(2,\Z)=\SL(2,\Z)/\Z_2.
$$
When thinking of $\PSL(2,\Z)$ instead of $\SL(2,\Z)$, one should
consider all $2\times2$ matrices up to a scalar multiple.
Note that the group $\PSL(2,\Z)$ is usually called the {\it modular group}.


%%%%%%%%%%%%%%%%%%%%
\subsection{Relation to continued fractions}\label{CFSec}
%%%%%%%%%%%%%%%%%%%%

Every rational number~$\frac{m}{n}>1$ can be written as a finite continued fraction
$$
\frac{m}{n}
\quad=\quad
a_1 + \cfrac{1}{a_2
          + \cfrac{1}{\ddots +\cfrac{1}{a_{k}} } },
          $$
with integer coefficients~$a_i$, such that $a_i\geq1$ for all $i\geq1$.
The standard notation is 
$$
\frac{n}{m}=[a_1,a_2,\ldots,a_{k}].
$$
The above continued fraction expansion is unique
if one chooses an even or odd number of coefficients.
For convenience, we assume that $k$ is odd.

Written in the matrix, or fractional-linear form, the above continued fraction reads
\begin{equation}
\label{MCF}
\frac{m}{n}=
R^{a_1}L^{a_2}R^{a_3}L^{a_4}\cdots{}R^{a_k}
\left(\frac{0}{1}\right).
\end{equation}
We conclude that the matrix~\eqref{PythaM} of a Pythagorean triple
corresponding to $\frac{n}{m}$ is given by
\begin{equation}
\label{Matmn}
X_{\frac{m}{n}}=
A\,X_0\,A^T,
\end{equation}
where $A=R^{a_1}L^{a_2}R^{a_3}L^{a_4}\cdots{}R^{a_k}$ and $X_0$ is as in~\eqref{X0}.

%%%%%%%%%%%%%%%%%%%%
%%%%%%%%%%%%%%%%%%%%
\section{A brief account on $q$-rationals}\label{RatSec}
%%%%%%%%%%%%%%%%%%%%
%%%%%%%%%%%%%%%%%%%%

The notion of $q$-deformed rationals was introduced in~\cite{MGOfmsigma}.
In this section, we briefly remind the definition and some properties
of $q$-rationals that will be useful for what follows.

%%%%%%%%%%%%%%%%%%%%
\subsection{An intrinsic definition}
%%%%%%%%%%%%%%%%%%%%

Given a rational number,~$\frac{m}{n}$, its $q$-deformation is a rational function in~$q$
$$
\left[\frac{m}{n}\right]_q=
\frac{\cN_{\frac{m}{n}}(q)}{\cD_{\frac{m}{n}}(q)},
$$
where the numerator $\cN_{\frac{m}{n}}$ and the denominator $\cD_{\frac{m}{n}}$
are polynomials in~$q$ that both depend on $m$ and $n$.
Assume that we know the $q$-analogue
of one point, for instance, 
$$
[0]_q:=0.
$$

More precisely, the $q$-rationals are characterized by the following two recurrence formulas
\begin{equation}
\label{RecRat}
\left[\frac{m}{n}+1\right]_q=
q\left[\frac{m}{n}\right]_q+1,
\qquad\qquad
\left[-\frac{n}{m}\right]_q=
-\frac{1}{q\left[\frac{m}{n}\right]_q}.
\end{equation}
Polynomials $\cN_{\frac{m}{n}}$ and $\cD_{\frac{m}{n}}$  
possess many interesting properties; see~\cite{MGOmn} and references cited therein.
In particular, they are monic  polynomials with positive integer coefficients.
An important property of unimodality was proved in~\cite{OgRa}.

%%%%%%%%%%%%%%%%%%%%
\subsection{The $q$-deformed $\PSL(2,\Z)$-action}
%%%%%%%%%%%%%%%%%%%%

The action is determined by two generators of $\PSL(2,\Z)$ represented by the matrices
\begin{equation}
\label{Gens}
R_{q}=
\begin{pmatrix}
q&1\\
0&1
\end{pmatrix},
\qquad\qquad
L_{q}=
\begin{pmatrix}
q&0\\
q&1
\end{pmatrix}
\end{equation}
considered up to a scalar multiple by a power of~$q$.
The map $\frac{m}{n}\mapsto\left[\frac{m}{n}\right]_q$ is characterized by the property
that it intertwines the $\PSL(2,\Z)$-action~\eqref{LFT} and the $\PSL(2,\Z)$-action
with the generators $R_q$ and $S_q$ as in~\eqref{Gens}.


%%%%%%%%%%%%%%%%%%%%
\subsection{An explicit formula}
%%%%%%%%%%%%%%%%%%%%

Given a rational number $\frac{m}{n}$ whose continued fraction expansion is
$\frac{m}{n}=[a_1,a_2,\ldots,a_k]$, where $k$ is odd.
A simple way to calculate a $q$-rational is to replace $R$ and $L$ in~\eqref{MCF} by
$R_q$ and $L_q$.
$$
\left[\frac{m}{n}\right]_q
=
R_q^{a_1}L_q^{a_2}R_q^{a_3}L_q^{a_4}\cdots{}R_q^{a_k}
\left(\frac{0}{1}\right).
$$
This can be viewed as one of several equivalent definitions:

\begin{ex}
The first interesting examples
$$
\left[\frac{5}{2}\right]_{q}=
\frac{1+2q+q^{2}+q^{3}}{1+q},
\qquad\qquad
\left[\frac{5}{3}\right]_{q}=
\frac{1+q+2q^{2}+q^{3}}{1+q+q^{2}}
$$
illustrates the fact that the numerator (and denominator) of the $q$-rational
$\frac{m}{n}$ depend both on~$m$ and~$n$.
Observe that the polynomials in  the numerators
of these $q$-rationals are precisely the polynomials $\cC$ and~$\cC^*$ 
from our first example.

\end{ex}

%%%%%%%%%%%%%%%%%%%%
\subsection{The total positivity property}\label{TPSec}
%%%%%%%%%%%%%%%%%%%%

An important property of the $q$-rationals is the fact that the $q$-deformation
``preserves the order'' of rationals 
in the following sense (see~\cite{MGOfmsigma}, Theorem~2).
Suppose we have two rationals~$\frac{m}{n}>\frac{m'}{n'}$, 
then the polynomial 
$$
\mathcal{P}_{\frac{n}{m},\frac{n'}{m'}}(q)=
\cN_{\frac{m}{n}}(q)\cD_{\frac{m'}{n'}}(q)-\cD_{\frac{m}{n}}(q)\cN_{\frac{m'}{n'}}(q)
$$
has positive integer coefficients.
This statement is topological in nature,
since every ordered set is endowed with a natural topology.
It was essential for e

[truncated by EPFLemma document preflight]
```
