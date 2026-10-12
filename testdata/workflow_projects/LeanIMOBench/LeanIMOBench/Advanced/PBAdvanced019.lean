import Mathlib

/-
For a real number $r$, let $A(r)$ denote the fractional part of $2r$ in its decimal representation. For a real number $r$ and a positive integer $n$, define $B(n,r)$ as
$$
B(n,r)=\sum_{k=1}^n A(kr).
$$
Find all positive real numbers $r$ such that $n(n+1)r - B(n,r)$ is a multiple of $n$ for all positive integers $n$.

Solution: $r$ is a positive integer.
-/
theorem PBAdvanced019
    (A : ℝ → ℝ) (hA : ∀ r, A r = Int.fract (2 * r))
    (B : ℕ → ℝ → ℝ) (hB : ∀ n r, B n r = ∑ k ∈ Finset.Icc 1 n, A (k * r)) :
    {r : ℝ | 0 < r ∧ ∀ n > 0, ∃ (m : ℤ), n * (n + 1) * r - B n r = n * m}
      = {(n : ℝ) | (n : ℕ) (_ : 0 < n)} := by sorry
