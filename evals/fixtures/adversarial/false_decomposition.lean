import Mathlib

/- The false helper must invalidate this decomposition without poisoning the true goal. -/
theorem adversarial_false_helper (n : ℕ) : n + 1 = n := by
  sorry

theorem adversarial_true_goal (n : ℕ) : n + n = 2 * n := by
  sorry
