import Mathlib

open Relation

/--
The single-step operation in the problem: either adding 2 or multiplying by 3.
-/
def operation (x x' : ℕ) : Prop :=
  x' = x + 2 ∨ x' = 3 * x

/--
A single step in the blackboard game, applied simultaneously to both numbers.
We define the relation on pairs of natural numbers.
-/
def single_step (p q : ℕ × ℕ) : Prop :=
  operation p.fst q.fst ∧ operation p.snd q.snd

/--
The pair `q` is reachable from `p` if it can be obtained by a finite sequence of steps.
This is the reflexive and transitive closure of the `single_step` relation.
-/
def reachable (p q : ℕ × ℕ) : Prop :=
  ReflTransGen single_step p q

/--
A pair of numbers `(a, b)` can eventually be made equal if there exists some number `x`
such that the state `(x, x)` is reachable from the initial state `(a, b)`.
-/
def eventually_equal (a b : ℕ) : Prop :=
  ∃ x : ℕ, reachable (a, b) (x, x)

/--
The property characterizing the pairs (a, b) that can eventually be made equal,
as given in the answer: Either both are even, or both are odd and congruent modulo 4.
-/
def good_pair (a b : ℕ) : Prop :=
  (a % 2 = 0 ∧ b % 2 = 0) ∨ (a % 2 = 1 ∧ b % 2 = 1 ∧ a % 4 = b % 4)

/--
The main theorem: Two distinct positive integers a and b can be made equal
if and only if they satisfy the `good_pair` property.
The hypothesis requires a and b to be positive and distinct, as stated in the problem.
Note: If a and b were allowed to be equal initially, `eventually_equal a a` would be trivally true
via zero steps, and the `good_pair` condition would still hold for any positive `a`.
-/
theorem PBAdvanced014 (a b : ℕ) (h_pos : a > 0 ∧ b > 0) (h_ne : a ≠ b) :
    eventually_equal a b ↔ good_pair a b := by sorry
