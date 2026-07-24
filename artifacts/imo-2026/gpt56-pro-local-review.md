# Independent pre-proof review of the canonical local IMO 2026 Lean project

**Review date:** 2026-07-16  
**Reviewer role:** Independent pre-proof source-fidelity, elaboration, and task-design reviewer  
**Canonical project:** `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/formalization`  
**Authoritative public source:** <https://www.imo-official.org/problems/2026/>  
**Baseline commit:** [`jsm28/IMOLean@3fc62b66ec02aa8446f3a1461f540ed93a74caa3`](https://github.com/jsm28/IMOLean/commit/3fc62b66ec02aa8446f3a1461f540ed93a74caa3)  
**Scope:** Statement fidelity, pinned-environment elaboration, trust-hole accounting, representation bridges, and task staging. No olympiad proof or answer classification was attempted.

## Executive summary

**Recommendation: GO for proof work in the declared task modes.** The canonical local project elaborates in its pinned environment, has exactly the intended nine holes, and each of P1-P6 is source-faithful. P1, P2, P3, P4, and P6 rely on explicit, defensible representation or indexing bridges described below; P5 is direct. No source correction blocks work.

This is not a claim that any problem is proved. Every theorem still depends on `sorryAx`. P3-P5 are also not theorem-only benchmark targets: each is a sound **two-stage determine task** whose first work item is to replace the intentionally unknown `answer` body, followed by the theorem proof. Their present answer holes are task inputs, not source omissions.

| Problem | Statement verdict | Task mode | Start recommendation |
| --- | --- | --- | --- |
| P1 | `APPROVED WITH EXPLICIT BRIDGE` | `CONCRETE THEOREM TASK` | GO: prove `IMO2026P1.result`. |
| P2 | `APPROVED WITH EXPLICIT BRIDGE` | `CONCRETE THEOREM TASK` | GO: prove `IMO2026P2.result`. |
| P3 | `APPROVED WITH EXPLICIT BRIDGE` | `TWO-STAGE DETERMINE TASK` | GO in two-stage mode: determine `answer`, then prove `result`; not theorem-only ready. |
| P4 | `APPROVED WITH EXPLICIT BRIDGE` | `TWO-STAGE DETERMINE TASK` | GO in two-stage mode: determine `answer`, then prove `result`; not theorem-only ready. |
| P5 | `APPROVED` | `TWO-STAGE DETERMINE TASK` | GO in two-stage mode: determine `answer`, then prove `result`; not theorem-only ready. |
| P6 | `APPROVED WITH EXPLICIT BRIDGE` | `CONCRETE THEOREM TASK` | GO: prove `IMO2026P6.result`. |

Compilation success was used only as evidence of elaboration. The verdicts come from an independent clause-by-clause comparison with the complete official statements and from separate game, termination, indexing, and vacuity analysis.

## Authoritative source and local-input verification

### Official source

- The byte-preserved official response is `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/official-problems-2026-07-16.html`.
- Recomputed SHA-256:

  ```text
  198784ca80ae7b27041f295f4a24cd3371fdcef0d26bdf84e1ec5274206482d0
  ```

  This exactly matches the supplied preservation digest.
- The preserved response contains the complete Day 1 and Day 2 problem sections. Its rendered MathJax formulas were inspected, not inferred from search snippets. The live public URL also rendered the heading “IMO 2026 Problems,” year 2026 selected, and the same six statements during this review.
- The readable derivative, `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/original-questions.md`, has no substantive disagreement with the rendered official source. It alpha-renames P1's move variables and terminal value (`x,y,A` rather than the rendered `m,n,M`) and compresses some prose, but preserves every mathematical qualifier and formula. No derivative correction is required beyond recording that alpha-renaming.
- The official page is primary. The derivative was used only to navigate the preserved content.

### Baseline and prior-review challenge

The public baseline commit exists and is the commit titled “Add IMO 2026 problems.” Its P1 file omits `/ Nat.gcd ...` from the second replacement. The canonical local `IMO2026/P1.lean:15` contains the required quotient and therefore is not subject to the baseline P1 counterexample.

The prior audit at `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/gpt56-pro-review.md` was treated as a hypothesis, with these conclusions:

1. **Baseline P1 defect independently confirmed, but repaired locally.** The upstream baseline accepts the all-2 constant no-op; the canonical local relation does not.
2. **The prior report's “local corrected P1 target” is stale for this canonical layout.** It describes a multiset formulation in `formalization/IMO2026.lean`; the current file at that path is only the six-import umbrella (`IMO2026.lean:1-8`). The canonical reviewed P1 is the labeled-board formulation at `IMO2026/P1.lean`.
3. **The prior P3-P5 `PARTIAL` labels were theorem-only benchmark dispositions, not defects in the declared local task modes.** The present review prompt explicitly declares answer-plus-proof staging. Under that contract, all three answer types and theorem relations are sound, so they are approved as two-stage tasks while remaining not theorem-only ready.
4. **P2 and P6 are independently reconfirmed.** P2 needs the stated Euclidean/simplex representation bridge; P6 needs the exact zero-/one-based substitution written below.

### Protected-input integrity

Before writing this report, the prompt, preserved source, derivative, prior report, blueprint, README, umbrella, and all six modules were hashed. No Lean module, prompt, blueprint, README, prior report, manifest, or unrelated file was edited. The parent LeanFlow worktree was already heavily dirty and the artifact directory was untracked; no change was reverted, staged, committed, or pushed.

## Pinned environment

| Item | Evidence | Resolved value |
| --- | --- | --- |
| Lean toolchain | `lean-toolchain:1` | `leanprover/lean4:v4.32.0-rc1` |
| Lake package | `lakefile.toml:1-2` | `IMO2026`, default target `IMO2026` |
| Auto-implicit policy | `lakefile.toml:4-9` | `autoImplicit = false`, `relaxedAutoImplicit = false` |
| Direct mathlib pin | `lakefile.toml:12-15` | `3b5afa97c31c95c69273cc3724eb50c78399405c` |
| Manifest mathlib resolution | `lake-manifest.json:4-13` | Same exact revision and input revision |
| Checked-out mathlib | `git -C .lake/packages/mathlib rev-parse HEAD` | Same exact revision; package worktree clean |
| Lean binary | `lake env lean --version` | Lean `4.32.0-rc1`, commit `b4812ae53eea93439ad5dce5a5c26591c31cb697`, arm64 macOS |
| Lake binary | `lake --version` | Lake `5.0.0-src+b4812ae` |
| Umbrella target | `IMO2026.lean:1-6` | Imports P1-P6 |

This review used the existing local manifest and checked-out dependency. It did not run `lake update` and did not consult a moving mathlib branch.

## Compilation and trust checks

### Required commands

The following required commands were run from the canonical project directory:

```sh
lake build
for f in IMO2026/P1.lean IMO2026/P2.lean IMO2026/P3.lean IMO2026/P4.lean IMO2026/P5.lean IMO2026/P6.lean; do lake env lean "$f"; done
rg -n "\\b(sorry|admit|axiom|opaque)\\b" --glob '*.lean'
```

Results:

- `lake build`: exit `0`; “Build completed successfully (8646 jobs).”
- Every individual `lake env lean` invocation: exit `0`.
- Trust scan: exit `0` because it found the expected matches; exactly nine matches, all `sorry`.
- No `admit`, explicit `axiom`, or `opaque` declaration was found.

### Complete trust-relevant compile diagnostics

| Module | Exit | Diagnostics |
| --- | ---: | --- |
| `IMO2026/P1.lean` | 0 | `IMO2026/P1.lean:28:8: warning: declaration uses sorry` |
| `IMO2026/P2.lean` | 0 | `IMO2026/P2.lean:18:8: warning: declaration uses sorry` |
| `IMO2026/P3.lean` | 0 | `IMO2026/P3.lean:68:4: warning: declaration uses sorry`; `IMO2026/P3.lean:70:8: warning: declaration uses sorry` |
| `IMO2026/P4.lean` | 0 | `IMO2026/P4.lean:85:4: warning: declaration uses sorry`; `IMO2026/P4.lean:87:8: warning: declaration uses sorry` |
| `IMO2026/P5.lean` | 0 | `IMO2026/P5.lean:14:4: warning: declaration uses sorry`; `IMO2026/P5.lean:16:8: warning: declaration uses sorry` |
| `IMO2026/P6.lean` | 0 | `IMO2026/P6.lean:13:8: warning: declaration uses sorry` |

The build replayed the same nine warnings. “Exit 0” therefore means **elaborates with admitted holes**, not “proved.”

### Exact trust-scan inventory

```text
IMO2026/P6.lean:16:  sorry
IMO2026/P3.lean:68:def answer : ℕ+ → ℝ := sorry
IMO2026/P3.lean:75:  sorry
IMO2026/P1.lean:31:  sorry
IMO2026/P4.lean:85:def answer : Set ℝ := sorry
IMO2026/P4.lean:88:  sorry
IMO2026/P2.lean:34:  sorry
IMO2026/P5.lean:14:def answer : Set (ℝ+ → ℝ+) := sorry
IMO2026/P5.lean:18:  sorry
```

An additional `#print axioms` probe reported `sorryAx` for every open answer/result. P2-P5 also report standard dependencies such as `propext`, `Classical.choice`, and `Quot.sound` through imported constructions. There is no project-declared axiom; the only untrusted project-specific dependency is the intentional `sorryAx` inventory.

### P1 all-2 regression probe

The following independent in-memory probe was compiled without editing a project file:

```lean
import IMO2026.P1
example : ¬ IMO2026P1.ValidMove (fun _ : Fin 2026 => 2) (fun _ : Fin 2026 => 2) := by
  rintro ⟨i, j, hij, hi, hj, hrest, hgcd, hlcm⟩
  norm_num at hlcm
```

Exit status: `0`. The corrected transition sends the selected pair `(2,2)` to `(2,1)`, not `(2,2)`.

## Problem 1

**Verdict:** `APPROVED WITH EXPLICIT BRIDGE`  
**Task mode:** `CONCRETE THEOREM TASK`  
**Lean target:** `IMO2026/P1.lean:28-31`

### Source-qualifier matrix

| Official qualifier | Lean clause | Assessment |
| --- | --- | --- |
| Exactly 2026 board places | Positions are `Fin 2026 → ℕ` at `:12`, `:19`, and `:25`. | Exact labeled representation. |
| Initially all 2026 entries are integers greater than 1 | `h0 : ∀ i, 1 < p₀ i` at `:28`. | Exact; naturals plus strict lower bound give positive integers. |
| Entries need not be different | No value-distinctness hypothesis. | Exact. |
| Choose two integers greater than 1 from different places | `∃ i j, i ≠ j ∧ 1 < p₁ i ∧ 1 < p₁ j` at `:13`. | Exact. |
| Other places stay unchanged | Universal clause at `:13`. | Exact. |
| First replacement is `gcd(m,n)` | `p₂ i = Nat.gcd ...` at `:14`. | Exact. |
| Second replacement is `lcm(m,n)/gcd(m,n)` | Quotient clause at `:15`. | Exact; this is the local correction. |
| Continue while a move is possible | `ValidOrNoMove` at `:17-20` permits a move, or equality only when no selectable pair exists. | Exact under the stuttering bridge. |
| Regardless of choices, after finitely many moves exactly one value exceeds 1 | `∀ p, ValidSeq p₀ p → ∃ j, ∃! k, 1 < p j k` at `:29`. | Exact quantifier dependency: the finite time may depend on the play. |
| The terminal value is independent of all choices | One `M` precedes every valid sequence and terminal time at `:30`. | Exact. |

### Bridges and transition analysis

1. **Labeled places versus an unordered blackboard.** `Fin 2026` labels physical places. The existential pair is ordered only to assign the two outputs; swapping witnesses swaps output placement, while gcd and lcm are symmetric. The mathematical board values and all conclusions are permutation-invariant.
2. **Finite termination via infinite terminal stuttering.** A finite maximal official play extends uniquely to a `ValidSeq` by repeating its terminal position. Conversely, a `ValidSeq` cannot stutter at a nonterminal state: the equality branch requires no two values greater than 1, and the corrected legal move cannot be a self-loop. A witness time `j` therefore gives a finite official stopping time. Once there is exactly one value greater than 1, no pair is selectable and every later position is equal.
3. **No hidden output-order restriction.** If the official replacement puts the quotient at the first selected place, use the reversed ordered witnesses `j,i`.

For a general hypothetical self-loop with selected values `x,y>1`, the first output equality gives `x = gcd(x,y)`, hence `x ∣ y` and `lcm(x,y)=y`. The second equality would then give `y = y/x`, impossible for `x>1` and `y>0`. The compiled all-2 probe is the required concrete regression case.

### Vacuity, omissions, and smallest correction

- `ValidSeq` is nonempty for every initial position: at each state choose a legal pair when one exists, otherwise stutter. Thus neither theorem conjunct is vacuous.
- The terminal branch permits zero values greater than 1 as an abstract normal form, but the theorem's first conjunct must prove that reachable plays end with exactly one; this correctly keeps a conclusion out of the transition definition.
- No positivity, distinct-place, unchanged-place, quotient, finiteness, exact-uniqueness, or choice-independence qualifier is omitted.
- **Smallest correction needed:** none.

## Problem 2

**Verdict:** `APPROVED WITH EXPLICIT BRIDGE`  
**Task mode:** `CONCRETE THEOREM TASK`  
**Lean target:** `IMO2026/P2.lean:18-34`

### Source-qualifier matrix

| Official qualifier | Lean clause | Assessment |
| --- | --- | --- |
| `ABC` is a plane triangle | Two-dimensional real Euclidean affine space at `:13-14`; `AffineIndependent ℝ ![A,B,C]` at `:18`. | Exact nondegenerate-plane representation. |
| `M,N` are midpoints of `AB,AC` | Equalities at `:19`. | Exact. |
| `K` strictly inside `BMC` | Witness at `:20`; interior membership at `:24`. | Exact. |
| `L` strictly inside `BNC` | Witness at `:21`; interior membership at `:25`. | Exact. |
| `K` strictly inside `ABL` | Witness at `:22`; interior membership at `:26`. | Exact. |
| `L` strictly inside `AKC` | Witness at `:23`; interior membership at `:27`. | Exact. |
| `∠KBA = ∠ACL` | `∠ K B A = ∠ A C L` at `:28`. | Exact vertices and middle-argument angle vertex. |
| `∠LBK = ∠LNC` | `:29`. | Exact. |
| `∠LCK = ∠BMK` | `:30`. | Exact. |
| `O` is circumcentre of `AKL` | `AffineIndependent_AKL` at `:31`; equality at `:32`. | Exact. |
| Prove `OM=ON` | `dist O M = dist O N` at `:33`. | Exact. |

### Representation witnesses

- Mathlib's `Triangle ℝ P` is an affinely independent ordered triple. Its `interior` is the set of affine combinations whose three weights lie strictly in `(0,1)`, exactly the strict interior of the convex hull.
- Mathlib notation `∠ p₁ p₂ p₃` is the undirected Euclidean angle at the middle point `p₂`; this matches ordinary olympiad angle notation.
- The explicit affine-independence hypotheses do not add a material condition. `BMC` and `BNC` are nondegenerate consequences of nondegenerate `ABC` and the midpoint equations. The source itself calls `ABL`, `AKC`, and `AKL` triangles and places points strictly inside or assigns a circumcentre to them, which already presupposes their nondegeneracy.
- The generic 2-dimensional inner-product affine torsor is the coordinate-free Euclidean plane. Vertex ordering does not change simplex interior or the undirected angles used here.

### Vacuity, omissions, and smallest correction

The extra witnesses are well-formedness evidence, not contradictory assumptions or a geometric strengthening. All four strict interiors and all three angle equalities are present. There is no orientation reversal, endpoint substitution, or missing nondegeneracy condition.

**Smallest correction needed:** none; the source comment at `:16-17` already records the key bridge.

## Problem 3

**Verdict:** `APPROVED WITH EXPLICIT BRIDGE`  
**Task mode:** `TWO-STAGE DETERMINE TASK`  
**Answer work item:** `IMO2026/P3.lean:68`  
**Proof work item:** `IMO2026/P3.lean:70-75`

### Source-qualifier matrix

| Official qualifier | Lean clause | Assessment |
| --- | --- | --- |
| Positive integer `n` | The theorem quantifies `n : ℕ+` at `:70`. | Exact. |
| Stick length 1 | Marks lie in `(0,1)` at `:16`, `:21`; endpoints `{0,1}` are added at `:50`; lengths are successive differences at `:53-56`. | Exact under endpoint bridge. |
| Liu first marks at most `n` points | `Strategy.points` and `card_points_le` at `:14-17`. | Exact. |
| Xiang then marks at most `n` points | Xiang finset and bound at `:21-22`, repeated under the theorem at `:71-72`. | Exact dependency after Liu's fixed marks. |
| All marked points are distinct | Finsets give within-player distinctness; `Disjoint` at `:22`/`:72` gives cross-player distinctness. | Exact. |
| Cut at every marked point | Sorted union with endpoints at `:47-56`. | Exact on legal mark sets. |
| Number of pieces | Index type has `#Liu + #Xiang + 1` elements throughout `:23-24`, `:32-33`, and `:55`. | Exact. |
| Liu claims first and turns alternate | `Even k` selects Liu at `:36-38`; Xiang acts on odd `k`; payoff sums even turns at `:63-65`. | Exact. |
| Only unclaimed pieces may be taken | Liu's return subtype excludes prior range at `:24`; `PlayValid` requires the complete play to be injective at `:44-45`. | Exact. |
| Public-history information | Liu sees Xiang's mark set and every prior claim at `:18-24`. | Exact. |
| Xiang adversarial play | Every legal realised Xiang stream is universally quantified at `:72-74`. | Exact under realised-stream bridge. |
| Liu's total length | Piece lengths at `:53-56`; sum of Liu's claimed pieces at `:59-65`. | Exact. |
| Largest guaranteed `c` for each `n` | `IsGreatest` at `:70-74`. | Exact maximum/guarantee relation. |
| Determine the value as a function of `n` | `answer : ℕ+ → ℝ` at `:68`. | Correct answer type; intentionally open. |

### Strategy, adversary, and bridge analysis

1. **Interior marks.** The source says “on the stick”; Lean uses `(0,1)`. Marking an endpoint creates no new piece. Because each player may mark *at most* `n` points, deleting any endpoint mark consumes no useful resource and changes neither pieces nor payoff; every effective source play has an interior-mark representative and conversely.
2. **Realised Xiang stream.** Xiang is represented by `xiangClaims : ℕ → Fin pieces`, not a separate strategy object. Against a fixed deterministic Liu strategy and fixed marks, every adaptive legal Xiang policy produces one realised stream, and every stream satisfying `PlayValid` can be played sequentially. Universal quantification over those streams therefore covers exactly the adversarial paths.
3. **Legality and completion.** `PlayValid` is injectivity of a map `Fin P → Fin P`, where `P` is the number of pieces. It both forbids re-claiming and, by finite equal cardinality, ensures every piece is claimed exactly once. Liu's `claims` field is typed to choose outside the prior range.
4. **Sorted-piece indexing.** On theorem inputs, disjoint interior marks imply the sorted endpoint list has exactly `P+1` entries. Thus the `getD` defaults in `playPieceLength` are unreachable; indices `i` and `i+1` denote the exact adjacent endpoints.
5. **Payoff objective.** All pieces are claimed and their lengths sum to 1. Xiang maximizing his own total is therefore equivalent to minimizing Liu's total, so the universal lower-bound guarantee models the source objective.

### Nonvacuity and task boundary

The implication from `PlayValid` is not vacuous. If the first `k<P` claims are distinct, an unused piece exists. Liu's typed strategy always selects one; at Xiang turns one can choose another. Induction constructs a legal full stream for every strategy and every legal pair of mark sets. `P≥1`, including when both mark sets are empty.

The `answer` body is intentionally the first olympiad work item. Its type and `IsGreatest` relation correctly stage the classification. The file must not be advertised as a theorem-only benchmark until `answer` is replaced by a verified concrete formula.

**Omissions:** none.  
**Smallest correction needed:** none.

## Problem 4

**Verdict:** `APPROVED WITH EXPLICIT BRIDGE`  
**Task mode:** `TWO-STAGE DETERMINE TASK`  
**Answer work item:** `IMO2026/P4.lean:85`  
**Proof work item:** `IMO2026/P4.lean:87-88`

### Source-qualifier matrix

| Official qualifier | Lean clause | Assessment |
| --- | --- | --- |
| Real angle `0°<θ<180°`, known before play | `0 < θ ∧ θ < π` and strategy existential inside the `θ` predicate at `:87`. | Exact under radian bridge. |
| Shan-Yu chooses an arbitrary paper triangle | `∀ t₀ : Triangle ℝ P` in `Winning` at `:82`, in a 2D Euclidean affine space at `:14-15`. | Exact. |
| Win test occurs on the current triangle before a cut | `WinsNow` at `:17-19`; `Winning` checks every played state including `k=0` at `:82`. | Exact under post-win-extension bridge. |
| At least one angle equals exactly `θ` | Existential over cyclic `Fin 3` indices at `:19`. | Exact; the three middle vertices are enumerated. |
| Otherwise Mulan chooses a perimeter nonvertex point | `Move.i`, `Move.p`, and strict betweenness `Sbtw` at `:21-27`. | Exact. |
| Cut to the opposite vertex | `Move.half` uses vertex `i` and a point strictly inside the opposite side at `:29-63`. | Exact. |
| Shan-Yu may retain either half | Boolean-by-proposition branch in `Move.half`; arbitrary `c : ℕ → Prop` at `:73`, `:82`. | Exact under realised-history bridge. |
| Mulan may use the full visible history | `Strategy` receives `Fin (k+1) → Triangle` at `:65-69`. | Exact. |
| Force a win after finitely many steps against every Shan-Yu play | `∀ t₀ c, ∃ k, WinsNow ...` at `:82`. | Exact pointwise finite reachability. |
| Determine all such `θ` | Winning-angle set equals `answer : Set ℝ` at `:85-87`. | Correct answer type and relation; intentionally open. |

### Game and termination bridges

1. **Degrees to radians.** Mathlib angles are real radians, so the source interval `(0°,180°)` is exactly `(0,π)` and exact angle equality is preserved.
2. **Strict perimeter point.** `Sbtw ℝ side₁ p side₂` means `p` lies on the closed affine segment and differs from both endpoints; it is precisely a nonvertex point in that side's relative interior. Every nonvertex perimeter point of a nondegenerate triangle lies on one such side.
3. **Realised binary history.** With Mulan's deterministic history-dependent strategy fixed, Shan-Yu's adaptive choices determine a truth-value sequence `c`; every truth-value sequence is a legal sequence of retained halves. Universal `c` therefore covers all adversarial policies.
4. **Continuation after a win.** `play` is total and synthetically continues beyond winning states, whereas the official game stops. This is extensionally exact: before the first winning state, the generated history is the official history; at the first win the existential objective is already satisfied, and later values are irrelevant.
5. **Finite semantics.** The theorem uses `∀ c, ∃ k`, allowing the finite time to depend on Shan-Yu's play. This is the ordinary reachability-game reading of “in finitely many steps.” After fixing Mulan's strategy the adversary is binary/finitely branching; if a uniform bound were desired, König compactness makes all-path eventual reachability equivalent to a finite bound on the no-win tree.

### Nonvacuity and task boundary

Every `Triangle` is nondegenerate by construction. A side midpoint is strictly between its endpoints, so a legal `Move` exists at every nonwinning state; `Move.half` proves both retained objects are nondegenerate triangles. Constant true/false adversary histories exist. Thus neither the strategy nor adversary quantification is vacuous.

`answer : Set ℝ` is the correct first-stage classification target. The theorem equates it with exactly the restricted winning-angle set. It is not theorem-only ready while that body is `sorry`.

**Omissions:** none.  
**Smallest correction needed:** none.

## Problem 5

**Verdict:** `APPROVED`  
**Task mode:** `TWO-STAGE DETERMINE TASK`  
**Answer work item:** `IMO2026/P5.lean:14`  
**Proof work item:** `IMO2026/P5.lean:16-18`

### Source-qualifier matrix

| Official qualifier | Lean clause | Assessment |
| --- | --- | --- |
| Domain and codomain are positive reals | `ℝ+ := Set.Ioi (0 : ℝ)` at `:11`; `f : ℝ+ → ℝ+` at `:14`, `:16`. | Exact. |
| Universal positive `x,y` | `∀ x y : ℝ+` at `:16`. | Exact. |
| `sqrt((x²+f(y)²)/2) ≥ (f(x)+y)/2` | `(f x + y)/2 ≤ √((x^2 + f y^2)/2)` at `:16`. | Exact; same inequality with sides exchanged. |
| `(f(x)+y)/2 ≥ sqrt(x f(y))` | `√(x * f y) ≤ (f x + y)/2` at `:17`. | Exact. |
| Both inequalities hold simultaneously | Conjunction across `:16-17`. | Exact. |
| Determine all such functions | Predicate-set equality to `answer : Set (ℝ+ → ℝ+)` at `:14-17`. | Exact answer type and classification relation; intentionally open. |

Subtype coercions place every occurrence in `ℝ` while retaining strict positivity in the function type. Both radicands are nonnegative on the quantified domain. Neither radical, function application, exponent, factor `1/2`, nor inequality orientation is missing.

The predicate is nonvacuous as a proposition over a nonempty function space; the open `answer` is an intentional classification work item, not a hidden theorem parameter. The file is sound as a two-stage determine task and not theorem-only ready until `answer` is concrete.

**Omissions:** none.  
**Smallest correction needed:** none.

## Problem 6

**Verdict:** `APPROVED WITH EXPLICIT BRIDGE`  
**Task mode:** `CONCRETE THEOREM TASK`  
**Lean target:** `IMO2026/P6.lean:13-16`

### Source-qualifier matrix

| Official qualifier | Lean clause | Assessment |
| --- | --- | --- |
| Infinite sequence of positive integers greater than 1 | `a : ℕ → ℕ` and `one_lt : ∀ i, 1 < a i` at `:13`. | Exact under zero-based indexing. |
| For every positive source index, the next term is greater than the current term | Candidate predicate `a n < m` at `:14`. | Exact after substitution. |
| Gcd with every earlier term is greater than 1 | `∀ i ≤ n, 1 < Nat.gcd m (a i)` at `:14`. | Exact inclusive prefix. |
| The next term is the smallest positive integer with those properties | `IsLeast {...} (a (n+1))` at `:14`. | Exact. Candidate positivity is redundant from `a n>1` and `a n<m`. |
| Positive integers `T,L` exist | `∃ T L : ℕ, 0<T ∧ 0<L` at `:15`. | Exact. |
| Additive periodicity for every positive source index | `∀ n, a (n+T)=a n+L` at `:15`. | Exact after substitution. |

### Exact zero-/one-based substitution

To avoid overloading the local identifier, write the official sequence as `A₁,A₂,…` and the Lean sequence as `b : ℕ → ℕ`:

```text
b(k) = A_(k+1)       for every k ≥ 0.
```

At Lean index `k`, the hypothesis says

```text
b(k+1) is least among m such that
b(k) < m and gcd(m,b(i)) > 1 for every 0 ≤ i ≤ k.
```

Substituting `b(k)=A_(k+1)` gives

```text
A_(k+2) is least among m such that
A_(k+1) < m and gcd(m,A_(i+1)) > 1 for i=0,…,k.
```

With the official index `n=k+1` (so `n≥1`), this is exactly the source recurrence for `A_(n+1)` against `A₁,…,A_n`.

For the conclusion, let official `n=k+1`:

```text
b(k+T) = b(k) + L
⇔ A_(k+T+1) = A_(k+1) + L
⇔ A_(n+T) = A_n + L.
```

As `k` ranges over all naturals, official `n` ranges over all positive integers. There is no lost initial case and no shift of `T` or `L`.

### Vacuity, omissions, and smallest correction

`IsLeast` includes both membership of `a(n+1)` in the candidate set and lower-boundedness against every candidate, so strict growth, every gcd condition, and leastness are all substantive. Naturals plus the strict lower bound make the source's candidate positivity automatic.

**Omissions:** none.  
**Smallest correction needed:** none; `:11-12` already states the bridge.

## Exact hole classification

| # | Location | Declaration | Classification | Intended work |
| ---: | --- | --- | --- | --- |
| 1 | `IMO2026/P1.lean:31` | `IMO2026P1.result` | Proof hole | Prove fixed P1 proposition. |
| 2 | `IMO2026/P2.lean:34` | `IMO2026P2.result` | Proof hole | Prove fixed P2 proposition. |
| 3 | `IMO2026/P3.lean:68` | `IMO2026P3.answer` | Answer hole | Determine the largest-guarantee formula. |
| 4 | `IMO2026/P3.lean:75` | `IMO2026P3.result` | Proof hole | Prove the determined formula is `IsGreatest`. |
| 5 | `IMO2026/P4.lean:85` | `IMO2026P4.answer` | Answer hole | Determine the winning-angle set. |
| 6 | `IMO2026/P4.lean:88` | `IMO2026P4.result` | Proof hole | Prove equality with the game predicate. |
| 7 | `IMO2026/P5.lean:14` | `IMO2026P5.answer` | Answer hole | Determine the full solution family. |
| 8 | `IMO2026/P5.lean:18` | `IMO2026P5.result` | Proof hole | Prove equality with the inequality predicate. |
| 9 | `IMO2026/P6.lean:16` | `IMO2026P6.result` | Proof hole | Prove fixed P6 proposition. |

Totals: **3 answer holes + 6 proof holes = 9 `sorry` occurrences**. No other trust escape hatch was found.

## Prioritized remediation queue

**Empty.** No source, elaboration, task-boundary, vacuity, indexing, or trust-inventory defect requires correction before work begins in the declared modes.

The following are workflow constraints, not remediation items:

- Do not describe any module as proved while `sorryAx` remains.
- Do not ingest P3-P5 as theorem-only benchmarks while their answer definitions are unknown.
- For P3-P5, derive and review the answer before treating the theorem proof as a fixed target; do not insert an unverified guess merely to remove the answer hole.
- Preserve the pinned toolchain and mathlib revision for subsequent proof checks.

## Final proof-start checklist

| Check | P1 | P2 | P3 | P4 | P5 | P6 |
| --- | --- | --- | --- | --- | --- | --- |
| Complete official statement checked | Yes | Yes | Yes | Yes | Yes | Yes |
| Pinned environment elaborates | Yes | Yes | Yes | Yes | Yes | Yes |
| All source qualifiers represented | Yes | Yes | Yes | Yes | Yes | Yes |
| Material bridge explicit and defensible | Yes | Yes | Yes | Yes | N/A (direct) | Yes |
| Nonvacuity/termination semantics checked | Yes | Yes | Yes | Yes | Yes | Yes |
| Hole structure matches declared mode | Yes | Yes | Yes | Yes | Yes | Yes |
| May safely begin declared work | **Yes** | **Yes** | **Yes: answer first** | **Yes: answer first** | **Yes: answer first** | **Yes** |
| Theorem-only benchmark ready now | Yes, as an unproved proof target | Yes, as an unproved proof target | **No** | **No** | **No** | Yes, as an unproved proof target |

**Final gate:** passed for all six modules under the declared task modes. P1, P2, and P6 may enter proof work directly. P3, P4, and P5 may enter answer-determination work, followed by proof work after their concrete answers receive the same source-fidelity review.

## Direct public links

- [Official IMO 2026 problem page](https://www.imo-official.org/problems/2026/)
- [`jsm28/IMOLean` repository](https://github.com/jsm28/IMOLean)
- [Pinned baseline commit `3fc62b66ec02aa8446f3a1461f540ed93a74caa3`](https://github.com/jsm28/IMOLean/commit/3fc62b66ec02aa8446f3a1461f540ed93a74caa3)
- Baseline files: [P1](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P1.lean), [P2](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P2.lean), [P3](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P3.lean), [P4](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P4.lean), [P5](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P5.lean), [P6](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P6.lean)
- Baseline environment files: [`lean-toolchain`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/lean-toolchain), [`lakefile.toml`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/lakefile.toml), [`lake-manifest.json`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/lake-manifest.json)
- Public correction/context links referenced by the local README: [PR #1](https://github.com/jsm28/IMOLean/pull/1), [PR #2](https://github.com/jsm28/IMOLean/pull/2), [PR #3](https://github.com/jsm28/IMOLean/pull/3)

