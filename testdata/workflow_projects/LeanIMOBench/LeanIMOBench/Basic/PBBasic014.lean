import Mathlib

-- In Shoreline Amphitheatre, 2024 Googlers each hold up to five flags of various colors. Any group of three Googlers will always have at least two people holding flags of the same color. Prove that one specific flag color is held by at least 200 Googlers.

variable (Googler Color : Type) (flags : Googler → Finset Color)

-- the main condition: Any group of three Googlers will always have at least two people holding flags of the same color
def tripleCond : Prop :=
  ∀ triple : Finset Googler, triple.card = 3 →
    ∃ g1 ∈ triple, ∃ g2 ∈ triple, g1 ≠ g2 ∧ ¬ Disjoint (flags g1) (flags g2)

theorem PBBasic014
    -- there are 2024 Googlers
    (num_googlers : Googler ≃ Fin 2024)
    -- each holding flags of up to 5 colors
    (num_flags : ∀ googler, (flags googler).card ≤ 5)
    (cond : tripleCond Googler Color flags) :
    -- one specific flag color is held by at least 200 Googlers.
    ∃ color : Color, Nat.card {g : Googler | color ∈ flags g } ≥ 200 := by sorry
