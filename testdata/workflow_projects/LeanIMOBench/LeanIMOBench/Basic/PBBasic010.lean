import Mathlib

/-
The numbers $\{ 1, 2, 3, \ldots , 2022 \}$ are partitioned into two sets $A$ and $B$ of size $1011$ each. Let $S_{AB}$ denote the set of all pairs $(a, b) \in A \times B$ where $a < b$, and let $S_{BA}$ denote the set of all pairs $(a, b) \in A \times B$ where $b < a$.

Prove that $\sum_{(a, b) \in S_{AB}} (b - a) \neq \sum_{(a, b) \in S_{BA}} (a - b)$."
-/

theorem PBBasic010 (A B : Finset ℕ) (hdisj : Disjoint A B) (hunion : A ∪ B = Finset.Icc 1 2022)
    (ha : A.card = 1011) (hb : B.card = 1011) :
    -- we don't need to do the restriction to S_AB / S_BA, it corresponds to
    -- natural number subtraction in Lean, because if a > b, then b - a = 0
    ∑ a ∈ A, ∑ b ∈ B, (b - a : ℕ) ≠
    ∑ a ∈ A, ∑ b ∈ B, (a - b : ℕ) := by sorry
