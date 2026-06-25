# EPFLemma Document Formalization Context

Source document: `docs/HiltonMilnerTheorem/source/A_short_proof_of_the_Hilton-Milner_theorem.tex`
Input request: `docs/HiltonMilnerTheorem` (directory)
Document kind: `latex`
Detected title: A short proof of the Hilton-Milner Theorem
Target Lean file: `DocFormalizationDemo/AShortProofOfTheHiltonMilnerTheorem/Main.lean`
Planner blueprint: `/Users/lmilikic/Desktop/runai_start/vllm012/EPFLemma/testdata/workflow_projects/DocFormalizationDemo/DocFormalizationDemo/AShortProofOfTheHiltonMilnerTheorem/Blueprint.md`
Extracted text cache: `/Users/lmilikic/Desktop/runai_start/vllm012/EPFLemma/testdata/workflow_projects/DocFormalizationDemo/.epflemma/workflow-state/formalization/docs-HiltonMilnerTheorem-source-A_short_proof_of_the_Hilton-Milner_theorem/extracted.txt`
Preflight manifest: `/Users/lmilikic/Desktop/runai_start/vllm012/EPFLemma/testdata/workflow_projects/DocFormalizationDemo/.epflemma/workflow-state/formalization/docs-HiltonMilnerTheorem-source-A_short_proof_of_the_Hilton-Milner_theorem/manifest.json`

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
12. During the source-fidelity drafting phase, prefer clarity and correctness over premature file splitting. It is acceptable to stabilize the first draft in one generated Lean file if that helps you read the source carefully and respond to verifier feedback.
13. After independent statement/source review passes, the runner will give one final organization pass before the formalizer exits. In that pass, decide whether the generated formalization should be split into multiple files, preserve every blueprint declaration/source mapping, update imports and `## Generated File Layout`, and run project-level Lean verification.
14. Do not check the proof-ready checklist item yourself. Leave it unchecked until independent statement/source review has approved every source entry.

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

- line 136: Shifting
- line 257: Discussion
- line 291: Uniqueness of the Hilton-Milner family
- line 365: Acknowledgements

## Detected Theorem-Like Blocks

1. `thm:HM` (protect, lines 91-95) - Hilton and Milner 1967 \cite{Hilton/Milner:1967}
   Source statement: Let $k\leq n/2$. If $\mathcal{F}$ is a family of pairwise-intersecting $k$-element subsets of $[n]$, where $\bigcap_{F\in\mathcal{F}}F=\emptyset$, then $\left|\mathcal{F}\right|\leq{n-1 \choose k-1}-{n-1-k \choose k-1}+1$.
2. `thm:MainTechnical` (protect, lines 106-115)
   Source statement: Let $2k-1\leq n$, let $\mathcal{A}$ be a family of $(k-1)$-element subsets of $[n]$, and let $\mathcal{B}$ be a family of $k$-element subsets of $[n]$. If $\mathcal{A},\mathcal{B}$ are cross-intersecting, $\mathcal{B}$ is nonempty, and $\bdry\mathcal{B}\subseteq\mathcal{A}$, then \[ \left|\mathcal{A}\right|+\left|\mathcal{B}\right|\leq{n \choose k-1}-{n-k \choose k-1}+1. \]
3. `lem:Frankl-Furedi` (protect, lines 201-206) - essentially Frankl and F�redi \cite{Frankl/Furedi:1986}
   Source statement: If $\mathcal{F}$ is a pairwise-intersecting family of $k$-element subsets of $[n]$ with $\bigcap_{F\in\mathcal{F}}F=\emptyset$, then there is a shifted family $\mathcal{F}'$ satisfying the same properties and with $\left|\mathcal{F}'\right|\geq\left|\mathcal{F}\right|$.
   Source proof excerpt: Given the lemma, the proof of Theorem~\ref{thm:HM} is nearly immediate. Let $\mathcal{F}$ be a shifted family satisfying the conditions of the theorem. Define \begin{alignat*}{2} \mathcal{A}= & \,\{F\setminus1\,\, & :F\in\mathcal{F}\text{ with }1\in\mathcal{F}\}\\ \mathcal{B}= & \,\{F & :F\in\mathcal{F}\text{ with }1\notin\mathcal{F}\} & . \end{alignat*} Since $\mathcal{F}$ is shifted, if $F\in\mathcal{F}$ does not have $1$, then $\left(F\setminus i\right)\cup1\in\mathcal{F}$ for each $i\in\mathcal{F}$. It follows that $\bdry\mathcal{B}\subseteq\mathcal{A}$. Since $\mathcal{F}$ is intersecting, also $\mathcal{A},\mathcal{B}$ are cross-intersecting systems of subsets of $\{2,\dots,n\}$. Since $\mathcal{F}$ has empty intersection, both of $\mathcal{A},\mathcal{B}$ are nonempty. The desired bound is now immediate from Theorem~\ref{thm:MainTechnical}.
   Source proof locator: lines 206-223
4. `line-224` (protect, lines 224-227)
   Source statement: This proof requires only the special case of Theorem~\ref{thm:MainTechnical} where the set systems are shifted.
5. `thm:StrictHM` (protect, lines 295-301)
   Source statement: In the situation of Theorem~\ref{thm:HM}, if $4\leq k<n/2$ and $\left|\mathcal{F}\right|$ achieves the upper bound, then there is some $k$-set $B$ and $i\notin B$ so that $\mathcal{F}$ consists of $B$ together with all $k$-sets that both contain $i$ and intersect $B$.
6. `line-328` (protect, lines 328-334)
   Source statement: Let $\mathcal{F}$ be a family of pairwise-intersecting $k$-element subsets of $[n]$ with the additional property that for any $F_{0}\in\mathcal{F}$, the intersection $\bigcap_{\mathcal{F}\setminus\{F_{0}\}}F$ is empty. Then there is a shifted family $\mathcal{F}'$ satisfying the same properties and with $\left|\mathcal{F}'\right|\geq\left|\mathcal{F}\right|$.
   Source proof excerpt: By the \emph{standard family}, we mean the shifted family with $A=\left\{ 2,\dots,k+1\right\} $, $A'=\left\{ 2,\dots,k,k+2\right\} $, and all $k$-element sets that both contain $1$ and intersect $A$ and $A'$. It is obvious that the standard family is at least as large as any family where all but two sets contain $1$. Given $\mathcal{F}$, we perform a sequence of shifts. If these terminate in a shifted family with the desired properties, then we are done. Otherwise, an operation results in a family without the additional property. Stopping just before this operation and relabeling elements, we have a family containing sets with $1$ and not $2$, with $2$ and not $1$, with both $1$ and $2$, and possibly the set $B=\{3,\dots,k+2\}$. We may assume without loss of generality that we have all sets containing both $1,2$ and intersecting with $B$. Since these sets do not have any common intersection other than $1,2$, the operations $\shift_{i\leftarrow j}$ over all $3\leq i<j$ preserve the additional property. After shifting over $3\leq i<j$, if we have only one set with $1$ and not $2$, or only one set with $2$ and not $1$, then we replace with the standard family. Otherwise, we have in the family $\{a,3,\dots,k+1\}$ and $\{a,3,\dots,k,k+2\}$ for $a=1,2$, along with all sets containing $\{1,2\}$ and intersecting $B$. In particular, the family contains as subfamilies both $\bdry\left\{ 1,\dots,k+1\right\} $ and $\bdry\left\{ 1,\dots,k,k+2\right\} $. Both subfamilies have empty intersection and are preserved under all shift operations, so we can now shift until the system stabilizes.
   Source proof locator: lines 334-363

## TeX Project Discovery

Selected `docs/HiltonMilnerTheorem/source/A_short_proof_of_the_Hilton-Milner_theorem.tex` from `docs/HiltonMilnerTheorem`; 0 included .tex file(s), 0 bibliography file(s), 4 referenced local asset file(s), 1 PDF file(s), 1 figure file(s), 2 TeX support file(s).

Included TeX files:
- [none]

Bibliography files:
- [none]

Local assets:
- `docs/HiltonMilnerTheorem/source/8_Users_russw_Documents_Research_mypapers_A_short_proof_of_the_Hilton-Milner_theorem_hamsplain.bst`
- `docs/HiltonMilnerTheorem/2411.02513.source`
- `docs/HiltonMilnerTheorem/bulavka_woodroofe2024_hilton_milner.images.txt`
- `docs/HiltonMilnerTheorem/bulavka_woodroofe2024_hilton_milner.txt`

Nearby PDF files:
- `docs/HiltonMilnerTheorem/bulavka_woodroofe2024_hilton_milner.pdf`

Figure/image files:
- `docs/HiltonMilnerTheorem/bulavka_woodroofe2024_hilton_milner.pdf`

TeX support files:
- `docs/HiltonMilnerTheorem/source/8_Users_russw_Documents_Research_mypapers_A_short_proof_of_the_Hilton-Milner_theorem_hamsplain.bst`
- `docs/HiltonMilnerTheorem/source/A_short_proof_of_the_Hilton-Milner_theorem.bbl`

Missing or external TeX inputs:
- `@path.tex`

## Source Excerpt

```text
\batchmode
\makeatletter
\def\input@path{{"/Users/russw/Documents/Research/mypapers/A short proof of the Hilton-Milner theorem/"}}
\makeatother
\documentclass[12pt,oneside,english,lowtilde]{amsart}
\usepackage[T1]{fontenc}
\usepackage[latin9]{inputenc}
\usepackage{babel}
\usepackage{url}
\usepackage{amstext}
\usepackage{amsthm}
\usepackage{amssymb}
\usepackage{geometry}
\geometry{verbose,tmargin=3cm,bmargin=3cm,lmargin=3cm,rmargin=3cm}
\usepackage[dvips,pdfusetitle,
 bookmarks=true,bookmarksnumbered=false,bookmarksopen=false,
 breaklinks=false,pdfborder={0 0 1},backref=false,colorlinks=false]
 {hyperref}
\usepackage{breakurl}

\makeatletter
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Textclass specific LaTeX commands.
\newlength{\lyxlabelwidth}      % auxiliary length 
\theoremstyle{plain}
\newtheorem{thm}{\protect\theoremname}
\theoremstyle{plain}
\newtheorem{lem}[thm]{\protect\lemmaname}
\theoremstyle{remark}
\newtheorem{rem}[thm]{\protect\remarkname}

%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% User specified LaTeX commands.
%% Latin modern fonts should prevent arXiv fuzziness
\usepackage{lmodern}

%% minimize spacing around wrap-figs
\setlength\intextsep{.5em}

\makeatother

\providecommand{\lemmaname}{Lemma}
\providecommand{\remarkname}{Remark}
\providecommand{\theoremname}{Theorem}

\begin{document}
\global\long\def\link{\operatorname{link}}%

\global\long\def\del{\operatorname{del}}%

\global\long\def\cone{\operatorname{Cone}}%

\global\long\def\depth{\operatorname{depth}}%

\global\long\def\shift{\operatorname{Shift}}%

\global\long\def\symdiff{\ominus}%

\global\long\def\shiftext{\shift^{\wedge}}%

\global\long\def\extalg{\bigwedge}%

\global\long\def\ff{\mathbb{F}}%

\global\long\def\bdry{\partial}%

% Default proof to no "Proof." at start
\renewcommand{\proofname}{\hskip-\labelsep\spacefactor3000 }
\title{A short proof of the Hilton-Milner Theorem}
\author{Denys Bulavka and Russ Woodroofe}
\thanks{Work of the first author is partially supported by the Israel Science
Foundation grant ISF-2480/20 and the AARMS postdoctoral fellowship.
Work of the second author is supported in part by the Slovenian Research
Agency (research program P1-0285 and research projects J1-9108, J1-2451,
J1-3003, and J1-50000).}
\address{Einstein Institute of Mathematics, Hebrew University, Jerusalem 91904,
Israel}
\curraddr{Department of Mathematics \& Statistics, Dalhousie University, 6297
Castine Way, PO BOX 15000, Halifax, NS, Canada, B3H 4R2}
\email{denys.bulavka@dal.ca}
\urladdr{\url{https://kam.mff.cuni.cz/~dbulavka/}}
\address{Univerza na Primorskem, Glagolja�ka 8, 6000 Koper, Slovenia}
\email{russ.woodroofe@famnit.upr.si}
\urladdr{\url{https://osebje.famnit.upr.si/~russ.woodroofe/}}
\begin{abstract}
We give a short and relatively elementary proof of the Hilton-Milner
Theorem.
\end{abstract}

\maketitle
The Hilton-Milner Theorem gives the maximum size of a uniform pairwise-intersecting
family of sets that do not share a common element.
\begin{thm}[Hilton and Milner 1967 \cite{Hilton/Milner:1967}]
\label{thm:HM}Let $k\leq n/2$. If $\mathcal{F}$ is a family of
pairwise-intersecting $k$-element subsets of $[n]$, where $\bigcap_{F\in\mathcal{F}}F=\emptyset$,
then $\left|\mathcal{F}\right|\leq{n-1 \choose k-1}-{n-1-k \choose k-1}+1$.
\end{thm}

In the current article, we will show Theorem~\ref{thm:HM} to follow
quickly from the following theorem, which we believe to be of some
independent interest. Two set systems $\mathcal{A}$ and $\mathcal{B}$
are \emph{cross-intersecting} if for every $A\in\mathcal{A},B\in\mathcal{B}$,
the intersection $A\cap B$ is nonempty. The \emph{shadow} $\bdry B$
of a $k$-element set $B$ consists of all the $(k-1)$-element subsets
of $B$; the shadow of a uniform set family is the union of the shadows
of its constituent sets, thus consists of all $(k-1)$-element subsets
of constituent sets.
\begin{thm}
\label{thm:MainTechnical}Let $2k-1\leq n$, let $\mathcal{A}$ be
a family of $(k-1)$-element subsets of $[n]$, and let $\mathcal{B}$
be a family of $k$-element subsets of $[n]$. If $\mathcal{A},\mathcal{B}$
are cross-intersecting, $\mathcal{B}$ is nonempty, and $\bdry\mathcal{B}\subseteq\mathcal{A}$,
then 
\[
\left|\mathcal{A}\right|+\left|\mathcal{B}\right|\leq{n \choose k-1}-{n-k \choose k-1}+1.
\]
\end{thm}

Note that the bound of Theorem~\ref{thm:HM} is attained with a single
$k$-element set $B$ that does not contain $1$, together with all
the $k$-element sets that contain $1$ and intersect $B$; the bound
of Theorem~\ref{thm:MainTechnical} is attained with a single $k$-element
set and all $(k-1)$-element sets that intersect it.

Our proof of Theorem~\ref{thm:HM} may be viewed as injective. Other
recent proofs of Theorem~\ref{thm:HM} were given in \cite{Frankl:2019,Hurlbert/Kamat:2018},
but instead of relying on a simple cross-intersecting type theorem,
both of these proofs rely on a certain ``partial complement'' operation.
In somewhat older work \cite{Frankl/Tokushige:1992} (see also \cite{Frankl:2016}),
Frankl and Tokushige gave a proof of Hilton-Milner from a different
cross-intersection theorem, but the proof is less elementary than
that of Theorem~\ref{thm:MainTechnical}, requiring the Sch�tzenberger-Kruskal-Katona
Theorem. A recent preprint of Wu, Li, Feng, Liu and Yu \cite{Wu/Li/Feng/Liu/Yu:2026}
(now published) gives a proof based on still another cross-intersecting
theorem, but the existing proofs of this underlying result also seem
to be somewhat more difficult than our approach.

\subsection*{Shifting}

We recall that a system $\mathcal{F}$ of subsets of $[n]$ is \emph{shifted}
if for each $i<j$, whenever $F\in\mathcal{F}$, $j\in F$ and $i\notin F$,
then also $\left(F\setminus j\right)\cup i\in\mathcal{F}$. Here,
we abuse notation to identify $i,j$ with the singleton subsets $\{i\},\{j\}$
where it causes no confusion. The \emph{(combinatorial)} \emph{shifting
operation} $\shift_{i\leftarrow j}$ is defined as 
\begin{align*}
\shift_{i\leftarrow j}\mathcal{F}= & \left\{ F\in\mathcal{F}:j\notin F\text{ or }i\in F\text{ or }\left(F\setminus j\right)\cup i\in\mathcal{F}\right\} \\
 & \cup\left\{ \left(F\setminus j\right)\cup i:F\in\mathcal{F}\text{ is such that }j\in F,i\notin F\right\} .
\end{align*}

It is well-known that repeated applications of $\shift_{i\leftarrow j}$
over $i<j$ will eventually reduce an arbitrary set system to a shifted
set system, that the operation preserves the cross-intersecting property,
and that $\bdry\shift_{i\leftarrow j}\mathcal{F}\subseteq\shift_{i\leftarrow j}\bdry\mathcal{F}$
\cite{Frankl:1987,Frankl:1991,Gerbner/Patkos:2019,Herzog/Hibi:2011}.

\subsection*{Proof of Theorem~\ref{thm:MainTechnical}}

We carry out a straightforward induction on $n$.
\begin{proof}
If $n=2k-1$, then the upper bound is ${n \choose k-1}$, and the
result follows by noticing that if a $(k-1)$-element set is in $\mathcal{A}$,
then its complement cannot be in $\mathcal{B}$ (and vice-versa).

For the inductive step, we may assume that $\mathcal{A}$ and $\mathcal{B}$
are shifted; otherwise, shift. Let $\mathcal{A}(\neg n),\mathcal{B}(\neg n)$
consist of the subsets in $\mathcal{A},\mathcal{B}$ (respectively)
that do not contain $n$. It is immediate that $\mathcal{A}(\neg n),\mathcal{B}(\neg n)$
are shifted, cross-intersecting, and satisfy the shadow condition.
Let $\mathcal{A}(n),\mathcal{B}(n)$ be obtained by taking the families
consisting of the subsets in $\mathcal{A},\mathcal{B}$ that contain
$n$, then deleting $n$ from each subset. It follows quickly from
definitions that $\mathcal{A}(n),\mathcal{B}(n)$ are shifted, cross-intersecting,
and satisfy the shadow condition. 

As $\mathcal{A}$ and $\mathcal{B}$ are shifted, so $\mathcal{A}(\neg n)$
and $\mathcal{B}(\neg n)$ are nonempty, and hence by induction 
\begin{equation}
\left|\mathcal{A}(\neg n)\right|+\left|\mathcal{B}(\neg n)\right|\leq{n-1 \choose k-1}-{n-1-k \choose k-1}+1.\label{eq:notN}
\end{equation}

For $\mathcal{A}(n),\mathcal{B}(n)$, there are a few easy cases:

If $\mathcal{A}(n)$ is empty, then (by the shadow condition) also
$\mathcal{B}(n)$ is empty.

If $\mathcal{B}(n)$ is empty, then since $\mathcal{B}$ is nonempty
and shifted, we have $\{1,\dots,k\}\in\mathcal{B}$. Since every set
in $\mathcal{A}(n)$ intersects with $\{1,\dots,k\}$, we get $\left|\mathcal{A}(n)\right|+\left|\mathcal{B}(n)\right|=\left|\mathcal{A}(n)\right|\leq{n-1 \choose k-2}-{n-1-k \choose k-2}$.

If $\mathcal{B}(n)$ is nonempty, then by induction it holds that
\begin{align}
\left|\mathcal{A}(n)\right|+\left|\mathcal{B}(n)\right| & \leq{n-1 \choose k-2}-{n-1-(k-1) \choose k-2}+1\nonumber \\
 & \leq{n-1 \choose k-2}-{n-1-k \choose k-2}.\label{eq:BnNonempty}
\end{align}
 The result now follows from (\ref{eq:notN}), the bound on $\left|\mathcal{A}(n)\right|+\left|\mathcal{B}(n)\right|$,
and the Pascal's Triangle identity.
\end{proof}

\subsection*{Proof of Theorem~\ref{thm:HM}}

We will use the following lemma of Frankl and F�redi:
\begin{lem}[essentially Frankl and F�redi \cite{Frankl/Furedi:1986}]
\label{lem:Frankl-Furedi}If $\mathcal{F}$ is a pairwise-intersecting
family of $k$-element subsets of $[n]$ with $\bigcap_{F\in\mathcal{F}}F=\emptyset$,
then there is a shifted family $\mathcal{F}'$ satisfying the same
properties and with $\left|\mathcal{F}'\right|\geq\left|\mathcal{F}\right|$.
\end{lem}

\begin{proof}
Given the lemma, the proof of Theorem~\ref{thm:HM} is nearly immediate.
Let $\mathcal{F}$ be a shifted family satisfying the conditions of
the theorem. Define 
\begin{alignat*}{2}
\mathcal{A}= & \,\{F\setminus1\,\, & :F\in\mathcal{F}\text{ with }1\in\mathcal{F}\}\\
\mathcal{B}= & \,\{F & :F\in\mathcal{F}\text{ with }1\notin\mathcal{F}\} & .
\end{alignat*}
Since $\mathcal{F}$ is shifted, if $F\in\mathcal{F}$ does not have
$1$, then $\left(F\setminus i\right)\cup1\in\mathcal{F}$ for each
$i\in\mathcal{F}$. It follows that $\bdry\mathcal{B}\subseteq\mathcal{A}$.
Since $\mathcal{F}$ is intersecting, also $\mathcal{A},\mathcal{B}$
are cross-intersecting systems of subsets of $\{2,\dots,n\}$. Since
$\mathcal{F}$ has empty intersection, both of $\mathcal{A},\mathcal{B}$
are nonempty. The desired bound is now immediate from Theorem~\ref{thm:MainTechnical}.
\end{proof}
\begin{rem}
This proof requires only the special case of Theorem~\ref{thm:MainTechnical}
where the set systems are shifted.
\end{rem}


\subsection*{Proof of Lemma~\ref{lem:Frankl-Furedi}}

For completeness, we also prove the lemma.
\begin{proof}
Given $\mathcal{F}$ as in Theorem~\ref{thm:HM}, apply shifting
operations $\shift_{i\leftarrow j}$. Each such operation preserves
the pairwise-intersecting property and cardinality, but may or may
not result in a system with a common element of intersection.

If a sequence of shifting operations ends in a shifted system with
empty intersection, then we are certainly done.

Otherwise, some $\shift_{i_{0}\leftarrow j_{0}}$ results in a system
where every set contains $i_{0}$. Thus, before this step, we have
a system $\mathcal{F}$ where every set contains either $i_{0}$ or
$j_{0}$. Relabel $i_{0}$ to $1$ and $j_{0}$ to $2$, and continue
applying $\shift_{i\leftarrow j}$ operations over all $3\leq i<j$.
Thus, after these additional shift operations, we have $\left\{ 1,3,\dots,k+1\right\} $
and $\left\{ 2,3,\dots,k+1\right\} $ in the system. Without loss
of generality (since every set in $\mathcal{F}$ contains $1$ or
$2$), we also have all $k$-element subsets containing $\left\{ 1,2\right\} $;
otherwise, add them. Thus, we have $\bdry\left\{ 1,\dots,k+1\right\} $
contained in our system. As $\bdry\left\{ 1,\dots,k+1\right\} $ has
empty intersection and is preserved under all further shift operations
(over $1\leq i<j$), the result follows.
\end{proof}

\subsection*{Discussion}

In addition to being short and direct, our proof is relatively elementary,
using only shifting theory. Indeed, we recover a completely elementary
proof of the restriction of the Hilton-Milner Theorem to shifted systems.

A main difficulty in proofs of Hilton-Milner and/or Erd\H{o}s-Ko-Rado
type results is relating systems of $(k-1)$-element subsets to systems
of $k$-element subsets. Our approach handles this with the shadow
containment condition of Theorem~\ref{thm:MainTechnical}.

Our motivation here comes partly from combinatorial algebraic topology.
In particular, the simplicial complex generated by a shifted family
of $k$-element sets has homology with generators in $\mathcal{B}$
(using notation as in the proof of Theorem~\ref{thm:HM}). Thus,
Lemma~\ref{lem:Frankl-Furedi} transforms the combinatorial property
of empty intersection into a homological property. Kalai comments
on similar connections between intersection theorems and homology
in \cite[Section 6.4]{Kalai:2002}.

The approach also gives a unified proof of the well-known Erd\H{o}s-Ko-Rado
Theorem. More concretely, if we relax the hypothesis of Theorem~\ref{thm:MainTechnical}
to allow $\mathcal{B}$ to be empty, then the corresponding bound
is $\left|\mathcal{A}\right|+\left|\mathcal{B}\right|\leq{n-1 \choose k-1}$.
Erd\H{o}s-Ko-Rado now follows from replacing Theorem~\ref{thm:MainTechnical}
with the relaxed cross-intersection theorem in the proof of Theorem~\ref{thm:HM}.
The proof is similar to (and only slightly more complicated than)
the standard inductive proof of Erd\H{o}s-Ko-Rado for shifted systems.

The approach also recovers uniqueness of the largest family for Theorem~\ref{thm:HM}
when $n/2>k\geq4$. Here, we strengthen the hypothesis of Theorem~\ref{thm:MainTechnical}
to require $\mathcal{B}$ to have at least two elements. We discuss
the details in the following section.

\subsection*{Uniqueness of the Hilton-Milner family}

As mentioned in the discussion, the same techniques give uniqueness
of the maximum family in Theorem~\ref{thm:HM}. We prove:
\begin{thm}
\label{thm:StrictHM}In the situation of Theorem~\ref{thm:HM}, if
$4\leq k<n/2$ and $\left|\mathcal{F}\right|$ achieves the upper
bound, then there is some $k$-set $B$ and $i\notin B$ so that $\mathcal{F}$
consists of $B$ together with all $k$-sets that both contain $i$
and intersect $B$.
\end{thm}

We require $k\geq4$ in order to avoid some technicalities. In particular,
there is another family achieving the bound for $k=3$. See \cite{Hurlbert/Kamat:2018}
for more details and a different argument.

As in the proof of Theorem~\ref{thm:HM}, we reduce to a shifted
family, and prove for a shifted family.

The proof for a shifted family requires a completely straightforward
modification of Theorem~\ref{thm:MainTechnical}. We obviously require
$k\geq4.$ We also strengthen the hypothesis to require $\left|\mathcal{B}\right|\geq2$,
replacing the condition that $\left|\mathcal{B}\right|\geq1$; with
the strengthened hypothesis, the inequality is strict. Then in the
proof, we may have $\left|\mathcal{B}(n)\right|$ empty or nonempty.
If empty, then since $\mathcal{B}$ has at least two elements, so
$\mathcal{A}(n)$ is strictly smaller than the given bound. If nonempty,
then the bound in (\ref{eq:BnNonempty}) is already strict so long
as $k\geq4$. In either case, the induction step yields a strict inequality.

Theorem~\ref{thm:StrictHM} follows for shifted families by applying
the variant of Theorem~\ref{thm:MainTechnical} with $\left|\mathcal{B}\right|\geq2$
to the same families as in the proof of Theorem~\ref{thm:HM}.

It remains only to reduce to shifted families. This reduction requires
a bit of care. We did not find the following lemma in the literature,
although we believe it to be known to experts in the field.
\begin{lem}
Let $\mathcal{F}$ be a family of pairwise-intersecting $k$-element
subsets of $[n]$ with the additional property that for any $F_{0}\in\mathcal{F}$,
the intersection $\bigcap_{\mathcal{F}\setminus\{F_{0}\}}F$ is empty.
Then there is a shifted family $\mathcal{F}'$ satisfying the same
properties and with $\left|\mathcal{F}'\right|\geq\left|\mathcal{F}\right|$.
\end{lem}

\begin{proof}[Proof.]
 By the \emph{standard family}, we mean the shifted family with $A=\left\{ 2,\dots,k+1\right\} $,
$A'=\left\{ 2,\dots,k,k+2\right\} $, and all $k$-element sets that
both contain $1$ and intersect $A$ and $A'$. It is obvious that
the standard family is at least as large as any family where all but
two sets contain $1$.

Given $\mathcal{F}$, we perform a sequence of shifts. If these terminate
in a shifted family with the desired properties, then we are done.
Otherwise, an operation results in a family without the additional
property. Stopping just before this operation and relabeling elements,
we have a family containing sets with $1$ and not $2$, with $2$
and not $1$, with both $1$ and $2$, and possibly the set $B=\{3,\dots,k+2\}$.

We may assume without loss of generality that we have all sets containing
both $1,2$ and intersecting with $B$. Since these sets do not have
any common intersection other than $1,2$, the operations $\shift_{i\leftarrow j}$
over all $3\leq i<j$ preserve the additional property.

After shifting over $3\leq i<j$, if we have only one set with $1$
and not $2$, or only one set with $2$ and not $1$, then we replace
with the standard family. Otherwise, we have in the family $\{a,3,\dots,k+1\}$
and $\{a,3,\dots,k,k+2\}$ for $a=1,2$, along with all sets containing
$\{1,2\}$ and intersecting $B$. In particular, the family contains
as subfamilies both $\bdry\left\{ 1,\dots,k+1\right\} $ and $\bdry\left\{ 1,\dots,k,k+2\right\} $.
Both subfamilies have empty intersection and are preserved under all
shift operations, so we can now shift until the system stabilizes.
\end{proof}

\subsection*{Acknowledgements}

We particularly thank D�niel Gerbner for several helpful comments
about preprints of the paper. We also thank Peter Frankl, Bal�zs Patk�s,
John Shareshian, and Tam�s Sz\H{o}nyi.

\bibliographystyle{8_Users_russw_Documents_Research_mypapers_A_short_proof_of_the_Hilton-Milner_theorem_hamsplain}
\bibliography{7_Users_russw_Documents_Research_mypapers_A_short_proof_of_the_Hilton-Milner_theorem_Master}

\end{document}
```
