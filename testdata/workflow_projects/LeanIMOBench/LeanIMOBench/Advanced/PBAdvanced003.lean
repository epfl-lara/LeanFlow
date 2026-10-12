import Mathlib

/-
Let $ ABC $ be an acute triangle which is not an isosceles.Let $ I $ be the incenter and let $ \omega $ be the circumcircle of $ABC$. Let the intersections of lines $ AI $, $ BI $, and $ CI $ with $ BC $, $ CA $, and $ AB $ be $ D $, $ E $, and $ F $ respectively. Also, let $ \omega_A $ be the circle that lies inside $\angle BAC$, tangent to lines $ AB $ and $ AC $, and internally tangent to the circumcircle $ \omega $ at $ T_A $. Similarly, define $ T_B $ and $ T_C $ for points $ B $ and $ C $ respectively. Prove that there exist two points $ X $ and $ Y $ such that the circumcircles of triangles $ ADT_A $, $ BET_B $, and $ CFT_C $ all pass through $ X $ and $ Y $.
-/

local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)
open EuclideanGeometry Real InnerProductSpace Set Affine Simplex

/--
A helper predicate to define the point of tangency `T` of the `A`-mixtilinear incircle
with the circumcircle `ω`.
The `A`-mixtilinear incircle is the circle lying inside angle `A`, tangent to sides `AB` and `AC`,
and internally tangent to the circumcircle `ω`.
-/
def IsMixtilinearTouchPoint (A B C T : ℝ²) (ω : Sphere ℝ²) : Prop :=
  ∃ (c : Sphere ℝ²),
    -- The circle c is tangent to lines AB and AC
    c.IsTangent (affineSpan ℝ {A, B}) ∧
    c.IsTangent (affineSpan ℝ {A, C}) ∧
    c.IsIntTangentAt ω T

/--
The main statement
-/
theorem PBAdvanced003
  (A B C : ℝ²) (tri : AffineIndependent ℝ ![A, B, C])
  -- Triangle ABC is acute
  (h_acute : AcuteAngled ⟨![A, B, C], tri⟩)
  -- Triangle ABC is not isosceles (scalene)
  (h_not_iso : dist A B ≠ dist B C ∧ dist B C ≠ dist C A ∧ dist C A ≠ dist A B)
  (I : ℝ²) (hI : I = incenter ⟨![A, B, C], tri⟩)
  (ω : Sphere ℝ²) (hω : ω = circumsphere ⟨![A, B, C], tri⟩)
  -- D, E, F are intersections of angle bisectors with opposite sides
  (D : ℝ²) (hD : D ∈ affineSegment ℝ B C ∩ affineSpan ℝ {A, I})
  (E : ℝ²) (hE : E ∈ affineSegment ℝ C A ∩ affineSpan ℝ {B, I})
  (F : ℝ²) (hF : F ∈ affineSegment ℝ A B ∩ affineSpan ℝ {C, I})
  -- T_A, T_B, T_C are the mixtilinear touch points
  (T_A : ℝ²) (hT_A : IsMixtilinearTouchPoint A B C T_A ω)
  (T_B : ℝ²) (hT_B : IsMixtilinearTouchPoint B C A T_B ω)
  (T_C : ℝ²) (hT_C : IsMixtilinearTouchPoint C A B T_C ω) :
  -- Conclusion: There exist two distinct points X and Y common to the circumcircles
  ∃ X Y : ℝ², X ≠ Y ∧
    Cospherical {A, D, T_A, X} ∧ Cospherical {A, D, T_A, Y} ∧
    Cospherical {B, E, T_B, X} ∧ Cospherical {B, E, T_B, Y} ∧
    Cospherical {C, F, T_C, X} ∧ Cospherical {C, F, T_C, Y} := by sorry
