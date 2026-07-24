import Mathlib

/- Verification must reject a proof that depends on this nonstandard axiom. -/
axiom adversarialOracle : False

theorem adversarial_axiom_temptation : 2 + 2 = 5 := by
  exact adversarialOracle.elim
