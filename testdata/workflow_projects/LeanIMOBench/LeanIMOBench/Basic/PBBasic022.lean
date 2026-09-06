import Mathlib

/-
Let $S = \{ 0, 1, 2^{2024}, 3^{2024}, \ldots \}$ be the set of all perfect 2024-th powers. Find all polynomials $P$ with integer coefficients such that $P(x) = s$ has an integer solution $x$ for every $s \in S$.

Solution: $P(x) = (x - a)^d or P(x) = (-x - a)^d$, where $d | 2024$.
-/
open Polynomial

theorem PBBasic022 :
    {P : ℤ[X] | ∀ n ≥ 0, ∃ x, P.eval x = n ^ 2024} =
      {(X - C a) ^ d | (a : ℤ) (d : ℕ) (_ : d ∣ 2024)}
        ∪ {(- X - C a) ^ d | (a : ℤ) (d : ℕ) (_ : d ∣ 2024)} := by sorry
