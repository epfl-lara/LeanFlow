import Mathlib

/-
For a positive integer $n$, let $A_{n}$ be the number of perfect powers less than or equal to $n$. Here, a perfect power is a number that can be expressed in the form $a^{b}$, where $a$ is a positive integer and $b$ is an integer greater than or equal to 2. Prove that there are infinitely many $n$ such that $A_{n}$ divides $n+2024$.
-/
open scoped Classical

theorem PBAdvanced001 (A : ℕ → ℕ)
    (hA : ∀ n, A n = (Finset.Icc 1 n |>.filter fun m => ∃ b a, 0 < a ∧ 2 ≤ b ∧ m = a ^ b).card) :
    {n : ℕ | 0 < n ∧ A n ∣ n + 2024}.Infinite := by sorry
