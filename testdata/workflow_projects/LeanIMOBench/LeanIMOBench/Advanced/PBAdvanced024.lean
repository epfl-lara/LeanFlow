import Mathlib

/-
Let $P$ be a function from the set $\mathbb{Q}$ of rational numbers to itself, and suppose that $P$ satisfies
$$(P(b-P(a))+a-P(b))(P(a+P(b-P(a)))-b)=0$$
for all rational numbers $a, b$. Prove that the set $\{P(a)+P(-a):a\in\mathbb{Q}\}$ is a finite set, and find the maximum possible number of elements in this set.

Solution: the maximum number of elements is 2.
-/
theorem PBAdvanced024
    (IsGood : (ℚ → ℚ) → Prop)
    (IsGood_def : ∀ P, IsGood P ↔ ∀ a b,
      (P (b - P a) + a - P b) * (P (a + P (b - P a)) - b) = 0) :
    (∀ P, IsGood P → {P a + P (- a) | (a : ℚ)}.encard < ⊤) ∧
      IsGreatest {{P a + P (-a) | (a : ℚ)}.encard | (P : ℚ → ℚ) (_ : IsGood P)} 2 := by sorry
