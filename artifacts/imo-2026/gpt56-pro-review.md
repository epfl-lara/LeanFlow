# Independent review of the IMO 2026 Lean translations

**Review date:** 2026-07-16  
**Official source:** <https://www.imo-official.org/problems/2026/>  
**Pinned upstream commit:** [`jsm28/IMOLean@3fc62b66ec02aa8446f3a1461f540ed93a74caa3`](https://github.com/jsm28/IMOLean/commit/3fc62b66ec02aa8446f3a1461f540ed93a74caa3)  
**Review scope:** Statement fidelity and benchmark readiness, not proof completion.

## Executive summary

The pinned six-problem set is **not ready to publish as a proving benchmark**.

| Problem | Verdict | Severity | Benchmark disposition |
| --- | --- | --- | --- |
| P1 | **INCORRECT** | Critical | Reject upstream target. Its move omits division by the gcd, admits no-op moves, and makes the termination claim false. |
| P2 | **APPROVED** | None | Admit as a concrete geometry target. |
| P3 | **PARTIAL** | High | Retain the game model, but block benchmark use until the largest guaranteed value is explicit. |
| P4 | **PARTIAL** | High | Retain the game model, but block benchmark use until the winning-angle set is explicit. |
| P5 | **PARTIAL** | High | Retain the functional-inequality predicate, but block benchmark use until the solution set is explicit. |
| P6 | **APPROVED WITH EXPLICIT BRIDGE** | Informational | Admit after documenting the zero-based/one-based indexing bridge. |

Only P2 and P6 are concrete upstream theorem statements that a proving agent can presently attempt. P1 is materially false for an encoding-specific reason. P3–P5 describe the question mechanics but do not encode the answer being requested: each defines `answer` using `sorry`, so the theorem refers to an unconstrained value or set rather than a mathematical characterization.

The local P1 replacement in `formalization/IMO2026.lean` fixes the quotient error. It is equivalent to the official statement modulo an explicit labeled-board-to-multiset bridge and the standard equivalence between “every play terminates” and “there is no infinite legal path and all reachable normal forms have the same unique non-one value.” It elaborates, but its theorem is still unproved (`sorry`).

## Authoritative source and pinned environment

### Source verification

- The complete rendered official page was inspected at <https://www.imo-official.org/problems/2026/>; it identified itself as “IMO 2026 Problems” with year 2026 selected.
- The byte-preserved response is `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/official-problems-2026-07-16.html`.
- Its independently recomputed SHA-256 is `198784ca80ae7b27041f295f4a24cd3371fdcef0d26bdf84e1ec5274206482d0`, exactly the supplied digest.
- A live `curl` re-fetch on the review date produced the same SHA-256.
- The Markdown derivative was used only for navigation. One naming correction is required: the official P1 uses move variables `m,n` and terminal value `M`, while `original-questions.md` uses `x,y` and `A`. This is alpha-renaming only; the formula and qualifiers agree. This report uses the official names when describing the source.

### Checkout and dependency pinning

The checkout at `/Users/lmilikic/Documents/Codex/2026-07-15/hey-can-you-add-a-new/work/IMOLean` was verified before review:

```text
git rev-parse HEAD
3fc62b66ec02aa8446f3a1461f540ed93a74caa3

origin
https://github.com/jsm28/IMOLean.git

lean-toolchain
leanprover/lean4:v4.32.0-rc1

Lean
4.32.0-rc1, arm64-apple-darwin24.6.0
Lean commit b4812ae53eea93439ad5dce5a5c26591c31cb697

Lake
5.0.0-src+b4812ae

mathlib
3b5afa97c31c95c69273cc3724eb50c78399405c
```

The pinned metadata is public in [`lean-toolchain`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/lean-toolchain) and [`lake-manifest.json`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/lake-manifest.json#L4-L9). The checkout was on `main` at the exact commit, not a moving revision, and the six reviewed files had no local modifications.

## Compilation and trust diagnostics

The following exact narrow checks were run from the pinned checkout:

```sh
lake env lean IMO/IMO2026P1.lean
lake env lean IMO/IMO2026P2.lean
lake env lean IMO/IMO2026P3.lean
lake env lean IMO/IMO2026P4.lean
lake env lean IMO/IMO2026P5.lean
lake env lean IMO/IMO2026P6.lean
```

| File | Exit | Complete trust-relevant output |
| --- | ---: | --- |
| `IMO2026P1.lean` | 0 | <code>IMO/IMO2026P1.lean:27:8: warning: declaration uses &#96;sorry&#96;</code> |
| `IMO2026P2.lean` | 0 | <code>IMO/IMO2026P2.lean:16:8: warning: declaration uses &#96;sorry&#96;</code> |
| `IMO2026P3.lean` | 0 | <code>IMO/IMO2026P3.lean:68:4: warning: declaration uses &#96;sorry&#96;</code>; <code>IMO/IMO2026P3.lean:70:8: warning: declaration uses &#96;sorry&#96;</code> |
| `IMO2026P4.lean` | 0 | <code>IMO/IMO2026P4.lean:85:4: warning: declaration uses &#96;sorry&#96;</code>; <code>IMO/IMO2026P4.lean:87:8: warning: declaration uses &#96;sorry&#96;</code> |
| `IMO2026P5.lean` | 0 | <code>IMO/IMO2026P5.lean:14:4: warning: declaration uses &#96;sorry&#96;</code>; <code>IMO/IMO2026P5.lean:16:8: warning: declaration uses &#96;sorry&#96;</code> |
| `IMO2026P6.lean` | 0 | <code>IMO/IMO2026P6.lean:13:8: warning: declaration uses &#96;sorry&#96;</code> |

A source scan found no `admit`, explicit `axiom`, or `opaque` declaration in the six files. The only trust-relevant diagnostics were the warnings above. Every file therefore **elaborates with `sorry`**; none of the six results is proved. In P3–P5, one `sorry` is additionally inside the data being determined, which is a statement-completeness failure rather than an ordinary proof placeholder.

## Problem 1

**Verdict:** `INCORRECT`  
**Severity:** Critical  
**Pinned file:** [`IMO/IMO2026P1.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P1.lean)

### Source-qualifier matrix

| Source qualifier | Lean clause | Assessment |
| --- | --- | --- |
| There are exactly 2026 board places. | `Fin 2026 → ℕ` at `IMO/IMO2026P1.lean:12`, `:18`, `:24`, `:27`. | Exact finite labeled representation. |
| Initially every entry is an integer greater than 1; repetitions are allowed. | `h0 : ∀ i, 1 < p₀ i` at `:27`. No value-distinctness condition is imposed. | Faithful. |
| A move selects two different places whose values are both greater than 1. | `∃ i j, i ≠ j ∧ 1 < p₁ i ∧ 1 < p₁ j` at `:13`. | Faithful. |
| Unselected places do not change. | The `∀ k` clause at `:13`. | Faithful. |
| The selected values become `gcd(m,n)` and `lcm(m,n) / gcd(m,n)`. | `p₂ i = gcd ...` and `p₂ j = lcm ...` at `:14`. | **Material mismatch:** division by `gcd` is absent. |
| Confucius continues while a move exists, then stops. | `ValidOrNoMove` at `:16-19` and `ValidSeq` at `:21-25`; stuttering is allowed only when no two values exceed 1. | Mechanically faithful to maximal play, conditional on a correct move relation. |
| Regardless of choices, after finitely many moves exactly one entry `M` exceeds 1. | First conjunct of `result` at `:27-29`. | Correct intended quantifier order, but false under the encoded move. |
| The terminal value `M` is independent of choices. | Second conjunct at `:29`. | The intended dependency is represented, but the false move relation invalidates the target. |

### Concrete failure

Let the initial position be constant: `p₀ i = 2` for every `i : Fin 2026`, and let `p j = p₀` for every time `j`. Choose any two distinct indices, for example `0` and `1`. The upstream relation accepts

```text
gcd(2,2) = 2
lcm(2,2) = 2
```

so `ValidMove p₀ p₀` holds. Consequently the constant infinite sequence is a `ValidSeq`. At every time it has 2026 entries greater than 1, never exactly one, contradicting `result`'s first conjunct. More generally, if `x ∣ y`, replacing `(x,y)` by `(gcd(x,y), lcm(x,y)) = (x,y)` is a no-op. The official quotient prevents this: for `(2,2)` the second output is `1`.

This is neither an indexing convention nor a harmless strengthening. It changes the transition system and makes the theorem false.

### Benchmark readiness and correction

The target is not benchmark-ready. Compilation merely accepts the false statement with `sorry`.

**Smallest correction:** change `IMO/IMO2026P1.lean:14` to

```lean
p₂ j = Nat.lcm (p₁ i) (p₁ j) / Nat.gcd (p₁ i) (p₁ j)
```

and add a regression check showing that the all-2 position cannot move to itself under the corrected relation.

## Problem 2

**Verdict:** `APPROVED`  
**Severity:** None  
**Pinned file:** [`IMO/IMO2026P2.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P2.lean)

### Source-qualifier matrix

| Source qualifier | Lean clause | Assessment |
| --- | --- | --- |
| `ABC` is a nondegenerate plane triangle. | Two-dimensional Euclidean affine space at `IMO/IMO2026P2.lean:13-14`; `AffineIndependent ℝ ![A,B,C]` at `:16`. | Faithful object class. |
| `M,N` are the midpoints of `AB,AC`. | Midpoint equalities at `:17`. | Exact. |
| `K` lies strictly inside `BMC`; `L` lies strictly inside `BNC`. | Triangle independence witnesses at `:18-19`; interior membership at `:22-23`. | Exact. |
| `K` also lies strictly inside `ABL`; `L` also lies strictly inside `AKC`. | Independence witnesses at `:20-21`; interior membership at `:24-25`. | Exact. |
| `∠KBA = ∠ACL`. | `:26`. | Exact vertex and ray order. Mathlib's `∠` is the undirected Euclidean angle, matching the source. |
| `∠LBK = ∠LNC`. | `:27`. | Exact. |
| `∠LCK = ∠BMK`. | `:28`. | Exact. |
| `O` is the circumcentre of triangle `AKL`. | `AffineIndependent_AKL` at `:29`; circumcenter equality at `:30`. | Exact. The explicit nondegeneracy witness makes the source's use of “triangle” well-typed. |
| Prove `OM = ON`. | `dist O M = dist O N` at `:31`. | Exact. |

The additional affine-independence hypotheses are explicit well-formedness witnesses for each `Triangle` object. They do not introduce a substantive geometric restriction beyond the source's nondegenerate-triangle and strict-interior language. Quantifier order is the ordinary universal quantification over a configuration followed by the conclusion; no positivity, distinctness, interior, or equality qualifier is omitted.

### Benchmark readiness and correction

This is a concrete theorem target. Its proof is `sorry`, but the statement itself is source-faithful and attemptable.

**Smallest correction:** no theorem change. Add a source comment documenting that `∠` is undirected and that the affine-independence arguments are representation witnesses, not extra olympiad hypotheses.

## Problem 3

**Verdict:** `PARTIAL`  
**Severity:** High  
**Pinned file:** [`IMO/IMO2026P3.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P3.lean)

### Source-qualifier matrix

| Source qualifier | Lean clause | Assessment |
| --- | --- | --- |
| `n` is positive. | The theorem uses `n : ℕ+` at `IMO/IMO2026P3.lean:70`. | Exact. |
| The stick has length 1. | Marks are in `(0,1)` at `:16`, `:21`; endpoints `{0,1}` are inserted at `:50`; piece lengths are endpoint differences at `:53-56`. | Faithful modulo the endpoint bridge below. |
| Liu first marks at most `n` points. | `Strategy.points` and `card_points_le` at `:14-17`. | Exact. |
| Xiang then marks at most `n` further points, all marks distinct. | Xiang's `Finset`, cardinality bound, and `Disjoint` condition at `:21-24` and again at `:70-73`. | Exact order and dependency. |
| Cut at all marks into finitely many pieces. | Sorted endpoints at `:47-50`; `#Liu + #Xiang + 1` piece indices at `:23-24`, `:32-33`, `:55`. | Exact under distinct interior marks. |
| Liu claims first; players alternate and may claim only an unclaimed piece. | `Even k` selects Liu at `:36-38`; Liu's choice has proof of not being in the prior range at `:23-24`; `PlayValid` requires the full play to be injective at `:40-45`. | Faithful. |
| Choices are made with the preceding public play known. | Liu's claim function receives Xiang's marks and all prior claims at `:18-24`. Xiang is universally represented by a realised claim stream at `:32`, `:70-74`. | Faithful under the realised-stream bridge below. |
| Liu's payoff is the total length of her claimed pieces. | `playPieceLength` at `:52-56`; even-turn sum `playLength` at `:58-65`. | Exact. |
| Determine the largest guarantee for every `n`. | `IsGreatest` at `:70-74`, but its candidate is `answer n`, with `answer := sorry` at `:67-68`. | **Requested answer absent.** |

### Representation bridges and vacuity check

The source says points “on the stick”; Lean restricts marks to the open interval `(0,1)`. Endpoint marks create no new piece and can be deleted without increasing the number of marks or changing any payoff. Conversely, every effective mark is interior. This is a defensible bridge, but it should be stated.

Xiang's adaptive policy is represented by an arbitrary realised stream `xiangClaims`. Against a fixed deterministic Liu strategy, every adaptive legal response has one realised stream, and every stream satisfying `PlayValid` is a legal realised play. Thus this representation does not remove adversarial responses.

The implication from `PlayValid` at `:73-74` is not globally vacuous. Liu's `claims` field always returns an unused index while fewer than all pieces have been claimed. At each Xiang turn an unused finite index also exists, so a legal injective completion can be constructed for every legal pair of mark sets.

### Omission and benchmark readiness

`def answer : ℕ+ → ℝ := sorry` does not state a formula. It names an unconstrained real for each `n`. The result therefore encodes “this placeholder value is greatest,” not the requested determination of that value. The game mechanics are useful, but the theorem is not a concrete answer benchmark.

**Smallest correction:** replace `answer` at `:68` with an explicit, independently source-verified formula `ℕ+ → ℝ`. The `IsGreatest` theorem shape can then remain unchanged.

## Problem 4

**Verdict:** `PARTIAL`  
**Severity:** High  
**Pinned file:** [`IMO/IMO2026P4.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P4.lean)

### Source-qualifier matrix

| Source qualifier | Lean clause | Assessment |
| --- | --- | --- |
| `θ` is a real angle with `0° < θ < 180°`, known before play. | Set comprehension `0 < θ ∧ θ < π` at `IMO/IMO2026P4.lean:87`; the existential strategy is inside the predicate for that `θ`. | Exact under the degrees-to-radians bridge. |
| Shan-Yu chooses an arbitrary initial nondegenerate triangle. | `Triangle ℝ P` in a two-dimensional Euclidean space at `:14-15`; `∀ t₀` in `Winning` at `:82`. | Exact. |
| Mulan wins immediately if any current angle is exactly `θ`. | `WinsNow` cycles through the three vertices at `:17-19`. | Exact equality and all vertices. |
| Otherwise Mulan chooses a nonvertex perimeter point and cuts to the opposite vertex. | `Move.i`, `Move.p`, and strict betweenness `Sbtw` at `:21-27`. | Exact; strict betweenness excludes both vertices. |
| Shan-Yu may retain either resulting triangle. | Both halves at `:29-63`; arbitrary binary choices `c : ℕ → Prop` in `play` and `Winning` at `:73-82`. | Exact adversarial choice. |
| Mulan's strategy may use all public history. | `Strategy` consumes the full finite triangle history at `:65-69`. | Exact information semantics. |
| Mulan must force a win after finitely many steps for every Shan-Yu play. | `∀ t₀ c, ∃ k, WinsNow ...` at `:79-82`. | Exact pointwise finite-win quantifier order. |
| Determine every winning `θ`. | Equality to `answer` at `:87`, but `answer : Set ℝ := sorry` at `:84-85`. | **Requested answer absent.** |

### Representation bridges and termination semantics

Mathlib's Euclidean angles are real radians, so `π` corresponds to `180°`. This is the required unit bridge.

The adversary function `c` is a realised binary history rather than a separate history-dependent strategy object. With Mulan's deterministic strategy fixed, universal quantification over all such histories covers every Shan-Yu response path.

`Strategy.play` is defined beyond a state where Mulan has already won. This does not weaken or strengthen the objective: `Winning` asks only that some finite prefix state satisfy `WinsNow`, and all later synthetic moves are irrelevant. The order “test for a win before making the next cut” is therefore preserved extensionally.

The strategy/adversary quantifiers are not vacuous: every nondegenerate triangle has strict side points, and `c` ranges over both possible retained halves at every stage.

### Omission and benchmark readiness

`answer := sorry` is an arbitrary set of reals. The file encodes the game predicate but not the requested classification. A prover is not given a concrete set to establish.

**Smallest correction:** replace `answer` at `:85` by an explicit, source-verified subset of `ℝ`; retain the current left-hand game predicate and document the radian bridge.

## Problem 5

**Verdict:** `PARTIAL`  
**Severity:** High  
**Pinned file:** [`IMO/IMO2026P5.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P5.lean)

### Source-qualifier matrix

| Source qualifier | Lean clause | Assessment |
| --- | --- | --- |
| Domain and codomain are the positive reals. | `ℝ+ := Set.Ioi 0` at `IMO/IMO2026P5.lean:11`; `f : ℝ+ → ℝ+` at `:14`, `:16`. | Exact positivity in both domain and codomain. |
| The inequality holds for every positive `x,y`. | `∀ x y : ℝ+` at `:16`. | Exact quantifier order. |
| `sqrt((x²+f(y)²)/2) ≥ (f(x)+y)/2`. | `(f x + y) / 2 ≤ √((x ^ 2 + f y ^ 2) / 2)` at `:16`. | Exact, with inequality orientation reversed syntactically but not logically. |
| `(f(x)+y)/2 ≥ sqrt(x f(y))`. | `√(x * f y) ≤ (f x + y) / 2` at `:17`. | Exact. |
| Determine all such functions. | Predicate-set equality at `:16-17`, but `answer : Set (ℝ+ → ℝ+) := sorry` at `:13-14`. | **Requested answer absent.** |

There is no vacuity in the predicate: positive inputs and outputs are represented by subtypes, and both radical inequalities are present for every pair. The only material incompleteness is the right-hand solution set.

### Benchmark readiness and correction

The file captures “which functions satisfy the question mechanics” but not “which functions they are.” Because `answer` is opaque, the equality is not a concrete classification theorem.

**Smallest correction:** replace `answer` at `:14` with the explicit, independently validated family of all solutions. Do not use an unverified candidate family merely to remove `sorry`.

## Problem 6

**Verdict:** `APPROVED WITH EXPLICIT BRIDGE`  
**Severity:** Informational  
**Pinned file:** [`IMO/IMO2026P6.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P6.lean)

### Source-qualifier matrix

| Source qualifier | Lean clause | Assessment |
| --- | --- | --- |
| An infinite sequence of positive integers, each greater than 1. | `a : ℕ → ℕ` and `one_lt : ∀ i, 1 < a i` at `IMO/IMO2026P6.lean:13`. | Exact under zero-based indexing. |
| For every positive source index, the next term is greater than the current term. | Membership in the `IsLeast` set includes `a n < m` at `:14`. | Exact after reindexing. |
| The next term has gcd greater than 1 with every earlier term. | `∀ i ≤ n, 1 < Nat.gcd m (a i)` at `:14`. | Exact after reindexing. |
| It is the smallest positive integer with those properties. | `IsLeast {...} (a (n+1))` at `:14`. | Exact. Explicit positivity of candidate `m` is redundant because `a n > 1` and `a n < m`. |
| There exist positive integers `T,L`. | `∃ T L : ℕ, 0 < T ∧ 0 < L` at `:15`. | Exact. |
| `a_{n+T} = a_n + L` for every positive `n`. | `∀ n, a (n+T) = a n + L` at `:15`. | Exact after reindexing. |

### Explicit indexing bridge

Let the Lean sequence be `b(k) = a_{k+1}` in the official one-based notation. Lean's condition at `n` says that `b(n+1) = a_{n+2}` is least among numbers greater than `b(n) = a_{n+1}` having nontrivial gcd with `b(i) = a_{i+1}` for every `i ≤ n`; this is exactly the official recurrence at source index `n+1`. Lean's conclusion

```text
∀ k ≥ 0, b(k+T) = b(k) + L
```

becomes the official conclusion for every positive index after substituting `n = k+1`.

No finiteness, leastness, positivity, gcd, or all-index qualifier is lost. The unused local notation `ℝ+` at `:11` is harmless.

### Benchmark readiness and correction

This is a concrete theorem target and is ready for a proving agent once the indexing bridge is recorded. Its `sorry` is solely the proof placeholder.

**Smallest correction:** no theorem change. Add the reindexing paragraph above as a source-fidelity comment or companion lemma.

## Local corrected P1 target

**Local file:** `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/formalization/IMO2026.lean`  
**Assessment:** Fixes the upstream defect; source-equivalent modulo explicit bridges; statement-ready but unproved.

The local file was checked separately with

```sh
lake env lean /Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/formalization/IMO2026.lean
```

It exited 0 and emitted only:

```text
/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/formalization/IMO2026.lean:32:8: warning: declaration uses `sorry`
```

### Clause assessment

- `P1Move` at `formalization/IMO2026.lean:7-12` uses the official pair
  `gcd(x,y)` and `lcm(x,y) / gcd(x,y)`.
- `source = {x,y} + rest` consumes two multiset occurrences. This precisely represents “different places,” including the case `x = y`: two equal values can be selected only when the multiset has multiplicity at least two.
- `P1Terminal` at `:13-14` requires one value `A > 1` and exactly 2025 ones, hence exactly 2026 entries and exactly one non-one value.
- `P1TerminatesFrom` at `:16-18` rules out every infinite sequence of legal moves. This is the direct path-based form of “regardless of choices, after finitely many moves.”
- `P1NormalForm` at `:20-21` combines finite reachability with absence of a legal next move.
- `P1Statement` at `:23-30` requires termination, existence of a terminal normal form, and a unique value `A` shared by every reachable normal form. The explicit existence conjunct prevents the independence clause from becoming vacuous.

### Equivalence classification

The local statement is **equivalent to the official P1 modulo two explicit representation bridges**:

1. A labeled board `Fin 2026 → ℕ` is mapped to the multiset of its values. Removing two occurrences is equivalent to selecting two distinct places; conversely, every two-occurrence multiset decomposition can be realised by two distinct indices. Permuting board places has no mathematical effect.
2. A maximal play is finite for every choice sequence exactly when there is no infinite legal path; its endpoint is a reachable normal form. Requiring all such normal forms to contain the same unique `A > 1` is the two requested conclusions. The local use of `A` rather than the official `M` is alpha-renaming only.

The local formulation is not materially stronger: its explicit no-infinite-path and normal-form clauses unpack the source's termination language. It is not weaker: it quantifies over all reachable normal forms and separately requires one to exist. It remains unproved, so “elaborates with `sorry`” must not be reported as a completed solution.

## Prioritized remediation queue for LeanFlow

1. **P0 — Replace or patch upstream P1 before any benchmark ingestion.** Insert the missing quotient and retain the all-2 no-op witness as a regression test. The local multiset target is a suitable statement-level replacement if LeanFlow prefers unlabeled boards.
2. **P0 — Make P3, P4, and P5 answers explicit.** Derive or obtain source-verified classifications, encode them as closed definitions, and allow `sorry` only in the theorem proof body—not in `answer`.
3. **P1 — Add semantic bridge documentation/tests.** Record P3's endpoint and realised-stream bridges, P4's radians and post-win-extension semantics, and P6's zero-based reindexing. Test small finite P3 plays and both P4 retained-half branches to guard indexing and turn order.
4. **P1 — Re-run the narrow compile gate and placeholder inventory.** Benchmark admission should fail when a `determine` problem's answer definition contains `sorry`, even if the file elaborates.
5. **P2 — Admit P2 and P6 as the current usable subset.** Preserve their exact pinned environment metadata and keep proof status separate from statement approval.

## Direct public links

- [Official IMO 2026 problem page](https://www.imo-official.org/problems/2026/)
- [`jsm28/IMOLean` repository](https://github.com/jsm28/IMOLean)
- [Pinned commit `3fc62b66ec02aa8446f3a1461f540ed93a74caa3`](https://github.com/jsm28/IMOLean/commit/3fc62b66ec02aa8446f3a1461f540ed93a74caa3)
- [Problem 1 Lean file](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P1.lean)
- [Problem 2 Lean file](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P2.lean)
- [Problem 3 Lean file](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P3.lean)
- [Problem 4 Lean file](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P4.lean)
- [Problem 5 Lean file](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P5.lean)
- [Problem 6 Lean file](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P6.lean)
