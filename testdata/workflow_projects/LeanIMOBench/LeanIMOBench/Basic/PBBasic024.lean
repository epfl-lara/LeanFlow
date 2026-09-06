import Mathlib

/-
Find all non-negative integers $a, b, c$ such that $20^a + b^4=2024^c$

Solution: $(x,y,z) = (0,0,0)$
-/
theorem PBBasic024 :
    {(a, b, c) : ℕ × ℕ × ℕ | 20 ^ a + b ^ 4 = 2024 ^ c}
      = {(0, 0, 0)} := by sorry
