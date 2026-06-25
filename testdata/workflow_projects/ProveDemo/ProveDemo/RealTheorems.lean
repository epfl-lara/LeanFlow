-- In this homework, please prove theorems marked by TODO, whose proof is given as `sorry`
-- As a strong hint, we indicate useful mathlib lemmas using `#check` statements.
import Mathlib
set_option linter.style.longLine false

-- low-level proofs refer to axioms that the structure satisfies. calc chains = or ≤
example (x y u v : ℝ) : ((x + u) + y) + v = v + ((y + x) + u) := by
  -- we will use calc as well as add_assoc and add_comm
  calc ((x + u) + y) + v
     = (y + (x + u)) + v := by simp [add_comm]
   _ = ((y + x) + u) + v := by simp [add_assoc]
   _ = v + ((y + x) + u) := by simp [add_comm]


-- example of tacticts with numbers
example (x y u v : ℝ) : x + u + y + v = v + y + x + u := by
  ring

-- https://en.wikipedia.org/wiki/Ring_(mathematics)#Definition

example (x y u : ℝ) : x * u * y = y * x * u := by
  ring_nf

example (x : ℝ) : x * x = x^2 := by
  ring -- there are functions that define repeated + and * operations in rings
       -- Repeated * is ^ and is expanded for constant exponents

example (x y : ℝ) : (x + y)^2 = x^2 + 2*x*y + y^2 := by
  ring

example (x y : ℝ) : (x - y)*(x + y) = x^2 - y^2 := by
  ring

example (x y : ℝ) : (x - y)*(x^2 + x*y + y^2) = x^3 - y^3 := by
  ring

example (x y c1 c2 : ℝ) (h1 : x ≤ y) (h2 : c1 ≤ c2) : 0.1 * x + c1 ≤ 0.1 * y + c2 := by
  linarith

-- see mathlib/Mathlib/Tactic/Linarith/Frontend.lean for explanation of linarith
-- decision procedure for implication validity (via UNSAT) or dense linear orders
-- based on Simplex

example (x y : ℝ) : (x^2 + y^2)^2 ≥ (x^2 - y^2)^2 := by
  nlinarith

example (x y : ℝ) (h : x * y ≥ 0) : (x + y)^2 ≥ x^2 + y^2 := by
  linarith -- even this works!

example (x y : ℝ) (nh : x * y ≠ 0) : (x + y)^2 ≠ x^2 + y^2 := by
  -- linearith -- does not work; out of the scope of procedure
  contrapose nh with h-- equalities are better than inequalities. This swaps negated goal and assumption
  ring_nf at h -- Progress. Q: why did it not cancel x^2?
  simp at h  -- check `simp?` to see what it used
  simp only [mul_eq_zero] -- simplify the goal
  assumption

example (x y : ℝ) (h : x * y ≠ 0) : (x + y)^2 ≠ x^2 + y^2 := by
  grind -- based on highly successful satisfiability modulo theory (SMT) techniques
  -- https://lean-lang.org/doc/reference/latest/The--grind--tactic/

example (x : ℝ) : x^2 ≥ 0 := by
  nlinarith -- grind does not work here. nlinarith adds a special trick for >= 0
            -- if someone ports nlsat from z3 to grind, this and many other things will work

example (x : ℝ) (h : x ≠ 0) : ∃ y, ¬ (x + y)^2 ≥ (x - y)^2 := by
  exists -x
  simp only [add_neg_cancel, ne_eq, OfNat.ofNat_ne_zero, not_false_eq_true, zero_pow, sub_neg_eq_add,
    ge_iff_le, sq_nonpos_iff, add_self_eq_zero]
  exact h

-- we want something with absolute value, so use Lean 4: Loogle "abs"
-- found it at point 29

example (x y : ℝ) : |x - y| ≥ 0 := by
  exact abs_nonneg (x - y)

/- --------------------------------------------------------------
           Lipschitz
-------------------------------------------------------------- -/

def isLipschitz (f : ℝ → ℝ) (L : ℝ) : Prop :=
    ∀ (x:ℝ) (y: ℝ), |f x - f y| ≤ L * |x-y|

#check abs_mul

theorem ex1 : isLipschitz (fun x => 2 * x) 2 := by
  intro x y
  simp only
  have h: 2*x - 2*y = 2*(x-y) := by ring
  rw [h]
  simp [abs_mul]

-- useful lemmas

#check abs_abs_sub_abs_le
#check one_mul

-- TODO: prove this
theorem absLipschitz1 : isLipschitz abs 1 := by
  sorry

-- show definition of continuous function from R to R from mathlib:
#check ContinuousAt
-- it uses maps and filters

-- Pedestrian definition of continuity:
def isContinuous (f : ℝ → ℝ) : Prop :=
  ∀ (x: ℝ) (ε: ℝ), ε > 0 → ∃ δ > 0, ∀ y : ℝ, |x - y| < δ → |f x - f y| < ε

#check div_pos
#check mul_lt_mul_of_pos_left
#check ne_of_gt

-- If the function is Lipschitz then it is continuous:
theorem Lipschitz_continuous (f : ℝ → ℝ) (L : ℝ) (Lpos : L > 0) (isLip : isLipschitz f L) : isContinuous f := by
  unfold isContinuous
  intros x eps epsPos
  let delt := eps / L
  exists delt
  have deltPos: delt > 0 := by
    unfold delt
    field_simp [div_pos, *]
    simp [*]
  have main: ∀ (y : ℝ), |x - y| < delt → |f x - f y| < eps := by
    intro y
    unfold delt
    have lipxy: |f x - f y| ≤ L * |x - y| := isLip x y
    intro xyDiff
    calc |f x - f y|
       ≤ L * |x - y| := lipxy
     _ < L * (eps / L) := by simp [*]
     _ = eps := by field_simp [*]
  exact ⟨deltPos, main⟩

def addF (f1 : ℝ → ℝ) (f2 : ℝ → ℝ) : ℝ → ℝ :=
  fun x : ℝ => (f1 x) + (f2 x)

#check norm_add_le
#check Real.norm_eq_abs

-- TODO: prove this
lemma abs_add_diff (a b c d : ℝ) :
    |(a + b) - (c + d)| ≤ |a - c| + |b - d| := by
  sorry

-- TODO: prove this
theorem addLipschitz (f1 : ℝ → ℝ) (L1 : ℝ) (h1 : isLipschitz f1 L1)
                    (f2 : ℝ → ℝ) (L2 : ℝ) (h2 : isLipschitz f2 L2) :
        isLipschitz (addF f1 f2) (L1 + L2) := by
  sorry

-- Now, we look at multiplication.

def mulF (f1 : ℝ → ℝ) (f2 : ℝ → ℝ) : ℝ → ℝ :=
  fun x : ℝ => (f1 x) * (f2 x)

-- The product of two Lipschitz functions is NOT Lipschitz in general
-- (e.g. f(x) = x is 1-Lipschitz but x*x = x² is not Lipschitz on ℝ).
-- However, if both functions are bounded then their product is Lipschitz

def isBounded (f : ℝ → ℝ) (M : ℝ) : Prop :=
  ∀ x : ℝ, |f x| ≤ M

#check abs_mul
#check mul_le_mul_of_nonneg_right
#check mul_le_mul_of_nonneg_left
#check abs_nonneg

-- Auxiliary triangle inequality for a product difference.
-- TODO: prove this. Hint: add and subtract a mixed term in the middle
lemma abs_mul_diff (f1 f2 : ℝ → ℝ) (x y : ℝ) :
    |f1 x * f2 x - f1 y * f2 y|
      ≤ |f2 x| * |f1 x - f1 y| + |f1 y| * |f2 x - f2 y| := by
  sorry

-- TODO: prove this
theorem mulLipschitz
    (f1 : ℝ → ℝ) (L1 M1 : ℝ) (h1 : isLipschitz f1 L1) (b1 : isBounded f1 M1)
    (f2 : ℝ → ℝ) (L2 M2 : ℝ) (h2 : isLipschitz f2 L2) (b2 : isBounded f2 M2) :
    isLipschitz (mulF f1 f2) (M2 * L1 + M1 * L2) := by
  sorry
