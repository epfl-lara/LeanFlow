import Mathlib

/-
Given a triangle $ABC$ with $AB < AC < BC$, let $I$ be the incenter
of triangle $ABC$, and let $M$ and $N$ be the midpoints of sides
$CA$ and $AB$, respectively. Let $K$ be the midpoint of the arc
$BC$ of the circumcircle of triangle $ABC$ which does not contain
$A$. Let $B' \neq C$ be the point where the line parallel to $AC$
and tangent to the incircle of triangle $ABC$ intersects side $BC$,
and similarly, let $C' \neq B$ be the point where the line parallel
to $AB$ and tangent to the incircle of triangle $ABC$ intersects
side $BC$. Find the value of $\angle NIM+\angle B'KC'$ in terms
of degree.

Answer: 180
-/
open Real EuclideanGeometry Affine.Simplex

local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)
local notation degrees "ᵒ" => (degrees * π / 180)

/--
Auxiliary definition for constructing the points B' & C'.
Assuming ω is the incircle of ABC, then `IsParaTangentFoot A B C B' ω` is true
if `B'` is the point where the line parallel to AC and tangent to ω intersects side BC, and not C.
-/
def IsParaTangentFoot (A B C B' : ℝ²) (ω : Sphere ℝ²) : Prop :=
  B' ≠ C ∧ Collinear ℝ {B', B, C} ∧
  ∃ t : AffineSubspace ℝ ℝ², t.Parallel (affineSpan ℝ {A, C}) ∧ ω.IsTangent t ∧ B' ∈ t

theorem PBAdvanced022
    -- a triangle ABC
    (A B C : ℝ²) (tri : AffineIndependent ℝ ![A, B, C])
    -- ABC satisfies the given side inequalities
    (h_side_len : dist A B < dist A C ∧ dist A C < dist B C)
    (I : ℝ²) (hI : I = incenter ⟨![A, B, C], tri⟩) -- I is the incenter
    -- M and N are the midpoints of sides CA and AB
    (M : ℝ²) (hM : M = midpoint ℝ C A)
    (N : ℝ²) (hN : N = midpoint ℝ A B)
    -- We will denote the incircle ω, and circumcircle Ω
    (ω : Sphere ℝ²) (hω : ω = insphere ⟨![A, B, C], tri⟩)
    (Ω : Sphere ℝ²) (hΩ : Ω = circumsphere ⟨![A, B, C], tri⟩)
    -- K is the midpoint of the arc BC of the circumcircle of triangle ABC which does not contain A
    (K : ℝ²) (hK : K ∈ Ω ∧ dist K B = dist K C ∧ (affineSpan ℝ {B, C}).SOppSide A K)
    -- B' ≠ B is the point where the line parallel to AC and tangent to the incircle of triangle ABC intersects side BC
    (B' : ℝ²) (hB : IsParaTangentFoot A B C B' ω)
    -- C' ≠ C be the point where the line parallel to AB and tangent to the incircle of triangle ABC intersects side BC
    (C' : ℝ²) (hC : IsParaTangentFoot A C B C' ω) :
    --  The value of $\angle NIM+\angle B'KC'$ in terms of degree is 180
    ∠ N I M + ∠ B' K C' = 180ᵒ := by sorry
