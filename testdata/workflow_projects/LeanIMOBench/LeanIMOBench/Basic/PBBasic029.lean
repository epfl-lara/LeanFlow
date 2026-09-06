import Mathlib

/-
Let $p$ and $n$ be integers with $0\le p\le n-2$. Consider a set
$S$ of $n$ lines in the plane such that no two of them are parallel
and no three have a common point. Denote by $I$ the set of intersections
of lines in $S$. Let $O$ be a point in the plane not lying on any
line of $S$. A point $X\in I$ is colored red if the open line segment
$OX$ intersects at most $p$ lines in $S$. What is the minimum number
of red points that is contained in $I$?
Answer: $\frac{(p + 1)(p + 2)}{2}$
-/
open Affine
abbrev Point := EuclideanSpace ℝ (Fin 2)

def Line := {l : AffineSubspace ℝ Point // Module.rank ℝ l.direction = 1}
local instance : Membership Point Line where mem l X := X ∈ l.1

/-
A set of lines is in a general position if no two lines are parallel, and no three lines are concurrent.
-/
abbrev GeneralPosition (lines : Set Line) : Prop :=
  (∀ l1 ∈ lines, ∀ l2 ∈ lines, l1 ≠ l2 → ¬ l1.1 ∥ l2.1) ∧
  (∀ l1 ∈ lines, ∀ l2 ∈ lines, ∀ l3 ∈ lines, l1 ≠ l2 → l1 ≠ l3 → l2 ≠ l3 →
    (l1.1 : Set Point) ∩ l2.1 ∩ l3.1 = ∅)

-- The set of all intersections of a set of lines
abbrev intersections (lines : Set Line) : Set Point :=
  { X : Point | ∃ l1 ∈ lines, ∃ l2 ∈ lines, l1 ≠ l2 ∧ X ∈ l1 ∧ X ∈ l2 }

-- A point avoids a set of lines if it is not contained in any
abbrev Point.avoids (lines : Set Line) (X : Point) := ∀ l ∈ lines, X ∉ l

-- A predicate claiming that there at most `k` lines strictly between `O` `X`
abbrev atMostBetween (lines : Set Line) (p : ℕ) (O X : Point) : Prop :=
  { line ∈ lines | ∃ inter : Point, Sbtw ℝ O inter X ∧ inter ∈ line }.encard ≤ p

-- All points in the plane that have at most `p` lines
abbrev redPoints (lines : Set Line) (p : ℕ) (O : Point) : Set Point :=
  atMostBetween lines p O

-- The minimum possible number of red intersections given `p` and `n`
noncomputable
def minRed (p n : ℕ) : ℕ := sInf {
  (intersections lines ∩ redPoints lines p O).encard |
  -- we consider all general positions of `n` lines
  (lines : Set Line) (_ : lines.encard = n) (_ : GeneralPosition lines)
  -- and all positions of the point `O`
  (O : Point) (_ : O.avoids lines)
}

theorem PBBasic029 (p n : ℕ) (le : p+2 ≤ n) :
  minRed p n = (p+1) * (p+2) / 2 := by sorry
