import Mathlib

/-
Determine, with proof, all positive integers $k$ such that
\[
\frac{1}{n+1} \sum_{i=0}^{n} \binom{n}{i}^k
\]
is an integer for every positive integer $n$.

Solution: all even positive integers $k$.
-/
theorem PBAdvanced029 :
    {(k : ℕ) | 0 < k ∧ ∀ n > (0 : ℕ), ∃ (m : ℕ),
      1 / (n + 1 : ℚ) * ∑ i ∈ Finset.range (n + 1), n.choose i ^ k = m }
    = {(k : ℕ) | 0 < k ∧ Even k} := by sorry
