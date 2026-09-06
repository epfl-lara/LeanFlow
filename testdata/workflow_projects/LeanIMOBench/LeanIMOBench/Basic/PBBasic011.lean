import Mathlib

-- Let $A\subset \{1,2,\ldots,2000\}$, $|A|=1000$, such that $a$ does not divide $b$ for all distinct elements $a,b\in A$. For a set $X$ as above let us denote with $m_{X}$ the smallest element in $X$. Find $\min m_{A}$ (for all $A$ with the above properties).
-- Answer: 64

variable (A : Finset ℕ)

def Acond := A ⊆ Finset.Icc 1 2000 ∧ A.card = 1000 ∧ ∀ a ∈ A, ∀ b ∈ A, a ≠ b → ¬ a ∣ b

theorem PBBasic011 : sInf { Finset.min A | (A : Finset ℕ) (_ : Acond A) } = 64 := by sorry
