import Mathlib

/-
Given a trapezoid $ABCD$ with $AB,CD$ as the two legs. Circle $(W_{1})$ passes through $A,B$, and $(W_{2})$ passes through $C,D$ so that they are tangent to each other. The inscribed angle on circle $W_1$ corresponding to the arc AB on the side opposite to C and D is alpha, and the inscribed angle on circle $W_2$ corresponding to the arc CD on the side opposite to  A and B is beta. Construct $(W_{3})$ passing through $A,B$, $(W_{4})$ passing through $C,D$ such that the inscribed angle on circle W3 corresponding to the arc AB on the side opposite to C and D is $\beta$, and the inscribed angle on circle $W_4$ corresponding to the arc CD on the side opposite to  A and B is b $\alpha$. Prove that $(W_{3}),(W_{4})$ are tangent to each other.
-/
open Real Affine Simplex EuclideanGeometry
local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)

-- A predicate that ABCD is a trapezoid with legs AB, CD
structure IsTrapezoid (A B C D : ℝ²) : Prop where
  nodup : [A,B,C,D].Nodup
  ncoll : ¬ Collinear ℝ {A,B,C,D}
  para : (affineSpan ℝ {B, C}) ∥ (affineSpan ℝ {D, A})
  order : (affineSpan ℝ {A, B}).WSameSide C D

-- A predicate that two sphere are tangent somehow (externally or internally)
def SphereTangent (s1 s2 : Sphere ℝ²) :=
  s1.IsExtTangent s2 ∨ s1.IsIntTangent s2 ∨ s2.IsIntTangent s1

theorem PBBasic030 (A B C D : ℝ²) (trapezoid : IsTrapezoid A B C D)
    (W₁ W₂ W₃ W₄ : Sphere ℝ²)
    (h_AW₁ : A ∈ W₁) (h_BW₁ : B ∈ W₁) (h_CW₂ : C ∈ W₂) (h_DW₂ : D ∈ W₂)
    (h_AW₃ : A ∈ W₃) (h_BW₃ : B ∈ W₃) (h_CW₄ : C ∈ W₄) (h_DW₄ : D ∈ W₄)
    (tangent_W : SphereTangent W₁ W₂) (α β : Angle)
    (X₁ X₂ X₃ X₄ : ℝ²) -- X₁ ... X₄ will be the points to measure the inscribed angles
    (h_XW₁ : X₁ ∈ W₁) (h_XW₂ : X₂ ∈ W₂) (h_XW₃ : X₃ ∈ W₃) (h_XW₄ : X₄ ∈ W₄)
    -- since a point measures inscribed angle of an arc opposite to the given point,
    -- X₁, X₃ should be on the same side as AB, and X₂, X₄ on the same side as CD
    (h_side_X₁ : (affineSpan ℝ {A, B}).SSameSide X₁ C)
    (h_side_X₂ : (affineSpan ℝ {C, D}).SSameSide X₂ A)
    (h_side_X₃ : (affineSpan ℝ {A, B}).SSameSide X₃ C)
    (h_side_X₄ : (affineSpan ℝ {C, D}).SSameSide X₄ A)
    (angle_X₁ : ∠ A X₁ B = α) (angle_X₂ : ∠ C X₂ D = β)
    (angle_X₃ : ∠ A X₃ B = β) (angle_X₄ : ∠ C X₄ D = α) :
    SphereTangent W₃ W₄ := by sorry
