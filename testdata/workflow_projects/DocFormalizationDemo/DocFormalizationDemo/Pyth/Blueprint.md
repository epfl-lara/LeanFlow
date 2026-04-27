# Formalization Blueprint: docs/PythagoreanPolynomialParametrization/pyth.tex

- Source: `docs/PythagoreanPolynomialParametrization/pyth.tex`
- Target Lean entry file: `DocFormalizationDemo/Pyth/Main.lean`
- Status: statement/source verification corrected; ready for managed prover queue.

## Planner Checklist

- [x] Identify definitions and notation that must exist before theorem statements.
- [x] Split large source theorems into Lean-sized lemmas.
- [x] Record source labels/pages/equations for every generated declaration.
- [x] Check local project and Mathlib names before introducing duplicates.
- [x] Verify drafted Lean statements match the source document.
- [x] Run independent statement/source verification review and apply corrections.
- [x] Record a natural-language proof strategy or source proof pointer for each theorem/lemma.
- [x] Hand stable `sorry` declarations to the managed prover queue.

## Import Plan

The Lean file uses:
- `Mathlib.NumberTheory.PythagoreanTriples` for `PythagoreanTriple` and classification theorems.
- `Mathlib.Algebra.MvPolynomial.Basic` for multivariate polynomials over `ℤ`.
- `Mathlib.Algebra.MvPolynomial.Eval` for `MvPolynomial.eval`.
- `Mathlib.Algebra.GCDMonoid.Basic` for UFD/gcd machinery needed by the first theorem proof.

The root module `DocFormalizationDemo.lean` imports `DocFormalizationDemo.Pyth.Main` so that `lake build` covers the formalization.

## Helper Definitions

- `T_x a b c`, `T_y a b c`, `T_z a b c` — the rational parametrization
  `T(a,b,c) = (c(a²-b²)/2, cab, c(a²+b²)/2)`.
- `IntegerValued4`, `IntegerValued4OnPositiveNonnegative`, `IntegerValued16` —
  predicates recording that the displayed rational formulas evaluate to integers
  on the parameter domains required by the source.
- `f_param x y z w`, `g_param x y z w`, `h_param x y z w` — the explicit
  integer-valued polynomials from Theorem line-194.
- `f_pos x y z w`, `g_pos x y z w`, `h_pos x y z w` — the explicit
  integer-valued polynomials from Remark line-240.
- `fourSquares`, `fourSquaresPos`, `f_pos16`, `g_pos16`, `h_pos16` — the
  four-square substitution version with unrestricted integer parameters.

## Source Statement Inventory

### line-143

- Kind: remark
- Source locator: `docs/PythagoreanPolynomialParametrization/pyth.tex:143-146`
- Planned Lean declarations:
  - `no_integer_polynomial_parametrization (n : ℕ)`
- Dependencies: `MvPolynomial`, `PythagoreanTriple`, UFD/gcd theory
- Formal statement review:
  - Source: There do not exist `f,g,h ∈ ℤ[x₁,…,xₙ]` for any `n` such that `(f,g,h)` parametrizes the set of PTs.
  - Lean: `¬∃ (f g h : MvPolynomial (Fin n) ℤ), {(x,y,z) | PythagoreanTriple x y z} = Set.range (fun a => (eval a f, eval a g, eval a h))`
  - Fidelity note: The formal statement quantifies over `n` and uses `MvPolynomial (Fin n) ℤ` to model `ℤ[x₁,…,xₙ]`. The range equality captures "parametrizes".
- Source qualifiers:
  - object class: integer-coefficient polynomial triple `f,g,h ∈ ℤ[x₁,…,xₙ]`
  - quantifier shape: for every finite arity `n`, no such triple exists
  - parameter domain: all integer assignments to the `n` variables
  - image condition: the range is exactly the set of all Pythagorean triples
- Lean coverage:
  - object class and arity: `MvPolynomial (Fin n) ℤ`
  - quantifier shape: theorem parameter `(n : ℕ)` with negated existential over `f g h`
  - parameter domain and image condition: `Set.range (fun (a : Fin n → ℤ) => ...)`
- Scope changes: none
- Statement verification status: approved
- Source proof / prover notes:
  - Proof uses that `ℤ[x]` is a UFD, takes `d = gcd(g,h)`, sets `φ=f/d`, `ψ=g/d`, `θ=h/d`.
  - Then `φ² = (θ+ψ)(θ-ψ)` with `gcd(θ+ψ, θ-ψ) ∈ {1,2}`.
  - It cannot be `2` because of PT `(3,4,5)` (odd first coordinate).
  - So `θ+ψ` and `θ-ψ` are coprime squares, giving `θ=(s²+t²)/2`, `ψ=(s²-t²)/2`.
  - Then `ψ` is divisible by `2`, contradicting PT `(4,3,5)` (odd second coordinate).
  - Prover will need UFD properties, polynomial gcd, and divisibility lemmas.

### line-194

- Kind: theorem
- Source locator: `docs/PythagoreanPolynomialParametrization/pyth.tex:194-203`
- Planned Lean declarations:
  - `T_x a b c`, `T_y a b c`, `T_z a b c`
  - `T_is_pythagorean a b c`
  - `T_integer_iff a b c`
  - `f_param x y z w`, `g_param x y z w`, `h_param x y z w`
  - `param_integer_valued`
  - `integer_valued_parametrization`
- Dependencies: `PythagoreanTriple.IsClassified` (Mathlib), parity/mod-2 arithmetic
- Formal statement review:
  - Source: There exist `f,g,h ∈ Int(ℤ⁴)` parametrizing all PTs, with explicit formulas.
  - Lean: `param_integer_valued` states that each displayed rational formula is integer-valued on all `ℤ⁴`; `integer_valued_parametrization` combines this integer-valuedness assertion with set equality between `{(x,y,z) | PythagoreanTriple x y z}` and the integer triples obtained by evaluating `f_param, g_param, h_param`.
  - Fidelity note: The draft models the displayed formulas as concrete `ℚ`-valued Lean functions and records membership in `Int(ℤ⁴)` by the predicate `IntegerValued4`. It does not bundle them as `MvPolynomial (Fin 4) ℚ`, but it now formally asserts the missing integer-valuedness condition from the source theorem.
- Source qualifiers:
  - object class: explicit integer-valued rational polynomial triple in `Int(ℤ⁴)`
  - parameter domain: all integer quadruples
  - codomain/output condition: integer triples
  - image condition: exactly the set of all Pythagorean triples
- Lean coverage:
  - object class: `IntegerValued4 f_param`, `IntegerValued4 g_param`, `IntegerValued4 h_param`
  - parameter domain: `∀ x y z w : ℤ` inside `IntegerValued4`
  - output condition: each value has an integer witness coercing to the rational value
  - image condition: set equality in `integer_valued_parametrization`
- Scope changes:
  - representation change: formulas are concrete `ℤ → ℤ → ℤ → ℤ → ℚ` functions rather than bundled `MvPolynomial (Fin 4) ℚ`; integer-valuedness and image equality are stated explicitly.
- Statement verification status: approved
- Source proof / prover notes:
  - Every primitive PT is `T₁(a,b)` or `T₂(a,b)`; this is `PythagoreanTriple.IsClassified` in Mathlib.
  - `2·T₂(a,b) = T₁(a+b, a-b)` — helper lemma.
  - Every PT is `T(a,b,c)` for some `a,b,c ∈ ℤ` — helper lemma.
  - `T(a,b,c)` is a rational solution of `x²+y²=z²` — `T_is_pythagorean`.
  - `T(a,b,c) ∈ ℤ³` iff `c` even or `a ≡ b (mod 2)` — `T_integer_condition`.
  - The condition is parametrized by `(y+zw, z-yw, 2x-xw)` — helper lemma.
  - Substituting into `T` yields `f_param, g_param, h_param`.

### line-240

- Kind: remark
- Source locator: `docs/PythagoreanPolynomialParametrization/pyth.tex:240-253`
- Planned Lean declarations:
  - `f_pos x y z w`, `g_pos x y z w`, `h_pos x y z w`
  - `positive_param_integer_valued`
  - `positive_pythagorean_parametrization`
  - `fourSquares`, `fourSquaresPos`
  - `f_pos16`, `g_pos16`, `h_pos16`
  - `positive_pythagorean_parametrization_integer_parameters`
- Dependencies: `T_x/T_y/T_z`, `T_is_pythagorean`, `T_integer_condition`, positivity
- Formal statement review:
  - Source: The set of positive PTs is parametrized by explicit formulas with `x,y,z > 0` and `w ≥ 0`; then the four-square theorem converts this to unrestricted integer parameters by replacing `w` with a sum of four squares and `x,y,z` with sums of four squares plus one.
  - Lean: `positive_param_integer_valued` states the displayed formulas are integer-valued on the stated positive/nonnegative domain. `positive_pythagorean_parametrization` states the constrained-domain set equality. `positive_pythagorean_parametrization_integer_parameters` states the four-square, unrestricted `Fin 16 → ℤ` parameter version.
  - Fidelity note: This now captures both parts of the source remark: the displayed positive-parameter formula and the integer-parameter conversion.
- Source qualifiers:
  - object class: displayed rational formulas integer-valued on the stated constrained domain
  - parameter domain: `x,y,z` positive integers and `w` nonnegative integer
  - image condition: exactly the set of positive Pythagorean triples
  - follow-on claim: four-square substitution gives unrestricted integer parameters
- Lean coverage:
  - constrained integer-valuedness: `positive_param_integer_valued`
  - constrained parameter domain and image condition: `positive_pythagorean_parametrization`
  - unrestricted integer-parameter conversion: `positive_pythagorean_parametrization_integer_parameters` with `Fin 16 → ℤ`
- Scope changes: none
- Statement verification status: approved
- Source proof / prover notes:
  - Positive PTs are `T(a,b,c)` with `a,b,c > 0`, `a > b`, and `c` even or `a ≡ b (mod 2)`.
  - Such triples are parametrized by `(y+(1+w)z, y, x+(1-w)²x)` with `x,y,z > 0`, `w ≥ 0`.
  - Substituting into `T` gives `f_pos, g_pos, h_pos`.
  - For unrestricted integer parameters, use the four-square theorem:
    replace `w` by `w₁²+w₂²+w₃²+w₄²`, and replace each of `x,y,z` by a sum of four squares plus `1`.
