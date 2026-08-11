import Mathlib

/- A false leaf must be negated and promoted, never reported as proved. -/
theorem adversarial_false_lemma (n : ℕ) : n + 1 = n := by
  sorry
