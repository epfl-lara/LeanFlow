import Mathlib

/-
Suppose $a, b, c$ are positive integers such that $2^a + 1 = 7^b + 2^c$. Find all possible values of $(a, b, c)$.

Solution: $(x,y,z) = (3,1,1), (6,2,4)$.
-/
theorem PBBasic023 :
    {(a, b, c) : ℕ × ℕ × ℕ | 0 < a ∧ 0 < b ∧ 0 < c ∧ 2 ^ a + 1 = 7 ^ b + 2 ^ c}
      = {(3,1,1), (6,2,4)} := by sorry
