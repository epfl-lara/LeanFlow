import Mathlib

open Topology Filter Set Polynomial Function Matrix Nat Real Complex
open MeasureTheory Metric MvPolynomial
open scoped InnerProductSpace

theorem numbertheory_3pow2pownm1mod2pownp3eq2pownp2
    (n : ℕ)
    (h_pos : 0 < n) :
    (3 ^ 2 ^ n - 1) % 2 ^ (n + 3) = 2 ^ (n + 2) := by
  sorry
