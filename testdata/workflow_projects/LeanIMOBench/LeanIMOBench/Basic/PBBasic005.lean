import Mathlib

/-
Let $P$ be a polynomial with real coefficients whose leading coefficient is $1$. Suppose that for all nonzero real numbers $x$, we have $P(x) + P(1/x) = \frac{P(x + 1/x) + P(x - 1/x)}{2}$. Determine all possibilities for $P$.

Solution: $P(x)= x^4 +ax^2 +6$, $P(x)=x^2$
-/
open Polynomial

theorem PBBasic005 :
    {P : ℝ[X] | P.Monic ∧ ∀ x ≠ 0, P.eval x + P.eval (1 / x)
      = 1 / 2 * (P.eval (x + 1 / x) + P.eval (x - 1 / x))}
    = {X ^ 4 + a • X ^ 2 + 6 | (a : ℝ) } ∪ {X ^ 2} := by sorry
