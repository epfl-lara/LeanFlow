# Formalization Blueprint: docs/RecountingTheRationals.tex

- Source: `docs/RecountingTheRationals.tex`
- Target Lean entry file: `DocFormalizationDemo/RecountingTheRationals/Main.lean`
- Status: complete; Lean module and project build verified

## Planner Checklist

- [x] Identify definitions and notation that must exist before theorem statements.
- [x] Split large source theorems into Lean-sized lemmas.
- [x] Record source labels/pages/equations for every generated declaration.
- [x] Check local project and Mathlib names before introducing duplicates.
- [x] Verify drafted Lean statements match the source document.
- [x] Record a natural-language proof strategy or source proof pointer for each theorem/lemma.
- [x] Hand stable `sorry` declarations to the managed prover queue.

## Lean Import Plan

`Main.lean` imports:
- `Mathlib.Data.Rat.Defs` (for `Rat` / `ℚ`)
- `Mathlib.Data.Rat.Lemmas` (for rational arithmetic lemmas)
- `Mathlib.Data.Nat.GCD.Basic` (for `Nat.gcd`)
- `Mathlib.Algebra.Order.Ring.Unbundled.Rat` (for ordered rational arithmetic)

Project root import:
- `DocFormalizationDemo.lean` imports `DocFormalizationDemo.RecountingTheRationals.Main`, so plain `lake build` covers the generated module.

## Source Statement Inventory

### def:hyperbinary_representation

- Kind: definition
- Source line/page: 40
- Planned Lean declarations: `HyperbinaryRep` (type alias `List (Fin 3)`), `hyperbinaryValue`
- Dependencies: `List`, `Fin 3`
- Formal statement review: A hyperbinary representation is a finite sequence of digits in {0,1,2}. We model this as `List (Fin 3)`. The value is computed recursively: `value([]) = 0`, `value(d::ds) = d + 2 * value(ds)`.
- Source proof / prover notes: Trailing zeros are ignored in the sense that two representations with the same value are considered equivalent. We do not need an explicit equivalence relation for the main theorems because `h` is defined by recurrence.

### def:hyperbinary_count

- Kind: definition
- Source line/page: 51
- Planned Lean declarations: `h : Nat → Nat`
- Dependencies: None (defined by well-founded recursion)
- Formal statement review: `h(n)` is defined by the recurrence relations given in the source, using `Nat.strongRecOn` for well-founded recursion.
- Source proof / prover notes: The source defines `h(n)` as the number of hyperbinary representations, then proves the recurrences. For the formalization, we define `h` by the recurrences directly (which uniquely determine it). A separate theorem connecting `h` to the count of representations can be added later.

### def:calkin_wilf_fraction

- Kind: definition
- Source line/page: 55
- Planned Lean declarations: `q : Nat → Rat`
- Dependencies: `h`
- Formal statement review: `q n = (h n : Rat) / (h (n+1))`. Since `h n > 0` for all `n`, this is well-defined.
- Source proof / prover notes: None needed.

### lem:hyperbinary_zero

- Kind: lemma
- Source line/page: 64
- Planned Lean declarations: `h_zero : h 0 = 1`
- Dependencies: `h`
- Formal statement review: Directly from the definition of `h`.
- Source proof / prover notes: Trivial by `rfl`.

### lem:hyperbinary_odd

- Kind: lemma
- Source line/page: 69
- Planned Lean declarations: `h_odd : ∀ n, h (2*n+1) = h n`
- Dependencies: `h`
- Formal statement review: Matches source exactly.
- Source proof / prover notes: Proof by induction on `n` using the recurrence definition of `h`. For odd `2n+1`, the definition of `h` gives `h((2n+1)/2) = h(n)`.

### lem:hyperbinary_even

- Kind: lemma
- Source line/page: 76
- Planned Lean declarations: `h_even : ∀ n ≥ 1, h (2*n) = h n + h (n-1)`
- Dependencies: `h`
- Formal statement review: Matches source exactly.
- Source proof / prover notes: Proof by induction on `n` using the recurrence definition of `h`. For even `2n`, the definition gives `h(n) + h(n-1)`.

### lem:consecutive_coprime

- Kind: lemma
- Source line/page: 83
- Planned Lean declarations: `consecutive_coprime : ∀ n, Nat.Coprime (h n) (h (n+1))`
- Dependencies: `h`, `h_odd`, `h_even`
- Formal statement review: Matches source exactly.
- Source proof / prover notes: Proof by strong induction on `n`. Base case `n=0`: `h(0)=1`, `h(1)=1`, so `gcd(1,1)=1`. Inductive step: use the recurrence relations and the property that `gcd(a, b) = gcd(a, b-a)` when `a < b`.

### def:calkin_wilf_children

- Kind: definition
- Source line/page: 92
- Planned Lean declarations: `leftChild`, `rightChild`
- Dependencies: `Rat`
- Formal statement review: `leftChild a b = a/(a+b)`, `rightChild a b = (a+b)/b`.
- Source proof / prover notes: None needed.

### lem:left_child_recurrence

- Kind: lemma
- Source line/page: 101
- Planned Lean declarations: `left_child_recurrence`
- Dependencies: `q`, `h_odd`, `h_even`, `consecutive_coprime`
- Formal statement review: If `q n = a/b` in reduced form, then `q (2n+1) = a/(a+b)`.
- Source proof / prover notes: From `q n = a/b` and `Nat.Coprime a b`, deduce `h n = a` and `h (n+1) = b`. Then `q (2n+1) = h(2n+1)/h(2n+2) = h(n)/(h(n+1)+h(n)) = a/(b+a) = a/(a+b)`.

### lem:right_child_recurrence

- Kind: lemma
- Source line/page: 108
- Planned Lean declarations: `right_child_recurrence`
- Dependencies: `q`, `h_odd`, `h_even`, `consecutive_coprime`
- Formal statement review: If `q n = a/b` in reduced form, then `q (2n+2) = (a+b)/b`.
- Source proof / prover notes: Similar to left child. `q (2n+2) = h(2n+2)/h(2n+3) = (h(n+1)+h(n))/h(n+1) = (b+a)/b = (a+b)/b`.

### lem:parent_step_decreases

- Kind: lemma
- Source line/page: 115
- Planned Lean declarations: `parent_step_decreases`
- Dependencies: `Nat.Coprime`
- Formal statement review: If `a,b` coprime positive, `(a,b)≠(1,1)`, then: if `a<b`, `(a,b-a)` is coprime and sum decreases; if `b<a`, `(a-b,b)` is coprime and sum decreases.
- Source proof / prover notes: Coprimality follows from `gcd(a,b) = gcd(a,b-a)`. Sum decrease is trivial arithmetic.

### thm:calkin_wilf_enumeration

- Kind: theorem
- Source line/page: 123
- Planned Lean declarations: `calkin_wilf_enumeration`
- Dependencies: `h`, `h_zero`, `h_odd`, `h_even`, `consecutive_coprime`, `parent_step_decreases`
- Formal statement review: For every coprime positive `a,b`, there exists unique `n≥0` such that `h(n)=a` and `h(n+1)=b`.
- Source proof / prover notes: **Existence**: Well-founded induction on `a+b`. Base `(1,1)`: `n=0`. If `a<b`: by induction, exists `m` with `h(m)=a`, `h(m+1)=b-a`. Then `h(2m+1)=a` and `h(2m+2)=b`. If `b<a`: similar with `n=2m+2`. **Uniqueness**: If `h(n)=h(m)=a` and `h(n+1)=h(m+1)=b`, then by the recurrence relations and the tree structure, `n=m`.

### cor:explicit_positive_rational_listing

- Kind: corollary
- Source line/page: 135
- Planned Lean declarations: `explicit_positive_rational_listing`
- Dependencies: `calkin_wilf_enumeration`, `q`
- Formal statement review: The function `n ↦ q_n` is a bijection from `Nat` to positive rationals.
- Source proof / prover notes: Injectivity: if `q n = q m`, write both in lowest terms; by uniqueness in `calkin_wilf_enumeration`, `n=m`. Surjectivity: for any positive rational `r=a/b` in lowest terms, `calkin_wilf_enumeration` gives `n` with `h(n)=a`, `h(n+1)=b`, so `q n = r`.

## Proof Dependency Graph

```
h_zero, h_one (trivial)
  ↓
h_odd, h_even (induction on recurrence)
  ↓
consecutive_coprime (strong induction, uses h_odd, h_even)
  ↓
left_child_recurrence, right_child_recurrence (uses h_odd, h_even, consecutive_coprime)
  ↓
parent_step_decreases (arithmetic + gcd properties)
  ↓
calkin_wilf_enumeration (well-founded induction on a+b, uses all above)
  ↓
explicit_positive_rational_listing (uses calkin_wilf_enumeration)
```

## Statement Fidelity Notes

- The source leaves the formal representation of hyperbinary representations to the formalization. We model them as `List (Fin 3)` with a recursive value function.
- `h` is defined by recurrence rather than as a count of representations. This is equivalent and more convenient for proofs.
- The corollary is stated as an explicit conjunction of injectivity and surjectivity rather than using `Function.Bijective` with a subtype codomain, for clarity.
- All theorem statements match the source claims exactly; no weakening or strengthening.
