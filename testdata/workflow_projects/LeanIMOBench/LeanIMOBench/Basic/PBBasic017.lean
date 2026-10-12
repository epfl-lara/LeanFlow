import Mathlib

/-
Find all non-negative integer $n$ such that $A_n = 1 + 3^{20(n^2+n+1)} + 9^{14(n^2+n+1)}$ is a prime number.

Solution: There is no such $n$.
-/
theorem PBBasic017
    (A : ℕ → ℕ)
    (hA : ∀ n, A n = 1 + 3 ^ (20 * (n ^ 2 + n + 1))
      + 9 ^ (14 * (n ^ 2 + n + 1))) :
    {n | (A n).Prime} = {} := by sorry
