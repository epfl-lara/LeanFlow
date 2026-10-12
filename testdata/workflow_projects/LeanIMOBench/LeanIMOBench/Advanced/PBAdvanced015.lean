import Mathlib

/-
Consider an acute triangle $ABC$ that is not isosceles. Let $H_0$, $E$, and $F$ be the feet of the perpendiculars dropped from vertices $A$, $B$, and $C$ to their opposite sides, respectively. Let $D$ be the point where the incircle of $\triangle ABC$ is tangent to side $ BC $. Denote the incenter and circumcenter of $\triangle ABC$ as $I$ and $O$, respectively. Let $K$ be the intersection of line $IO$ and line $BC$. Let $Q$ be the point where the ray $IH_0$ intersects the circumcircle of $\triangle ABC$ again. Let $X$ be the point where the line $ QD $ intersects the circumcircle of $\triangle ABC$ at a point other than $Q$.
Let $Y$ be the point where the circle that touches rays $AB$, $AC$, and is also externally tangent to the circumcircle of $\triangle ABC$, touches the circumcircle of $ \triangle ABC$. Prove that if segment $EF$ is tangent to the incircle of $ \triangle ABC$, then $X$, $Y$, and $K$ are collinear.
-/
open Affine.Simplex EuclideanGeometry
local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)

/-- The set of points forming the ray $\overrightarrow{p₁ p₂}$. -/
def ray (p₁ p₂ : ℝ²) : Set ℝ² :=
  { AffineMap.lineMap p₁ p₂ t | (t : ℝ) (_ : t ≥ 0) }

theorem PBAdvanced015
    -- a triangle ABC
    (A B C : ℝ²) (tri : AffineIndependent ℝ ![A, B, C])
    -- ABC is acute
    (h_acute : AcuteAngled ⟨![A, B, C], tri⟩)
    -- ABC is non-isosceles
    (n_isosceles : [dist A B, dist A C, dist B C].Nodup )
    (H₀ : ℝ²) (hH₀ : H₀ = altitudeFoot ⟨![A, B, C], tri⟩ 0) -- H₀ is the foot from A
    (E : ℝ²) (hE : E = altitudeFoot ⟨![A, B, C], tri⟩ 1) -- E is the foot from B
    (F : ℝ²) (hF : F = altitudeFoot ⟨![A, B, C], tri⟩ 2) -- F is the foot from C
    -- Let $D$ be the point where the incircle of $\triangle ABC$ is tangent to side $ BC $.
    (D : ℝ²) (hD : D = touchpoint ⟨![A, B, C], tri⟩ ∅ 0)
    (I : ℝ²) (hI : I = incenter ⟨![A, B, C], tri⟩) -- I is the incenter
    (O : ℝ²) (hO : O = circumcenter ⟨![A, B, C], tri⟩) -- O is the circumcenter
    -- Let $K$ be the intersection of line $IO$ and line $BC$.
    (K : ℝ²) (hK : Collinear ℝ {K, I, O} ∧ Collinear ℝ {K, B, C})
    -- We denote the circumcircle of ABC as ω
    (ω : Sphere ℝ²) (hω : ω = circumsphere ⟨![A, B, C], tri⟩)
    -- Let $Q$ be the point where the ray $IH_0$ intersects the circumcircle of $\triangle ABC$ again.
    (Q : ℝ²) (hQ : Q ∈ ray I H₀ ∩ ω)
    -- Let $X$ be the point where the line $ QD $ intersects the circumcircle of $\triangle ABC$ at a point other than $Q$.
    (X : ℝ²) (hX : X ≠ Q ∧ X ∈ (affineSpan ℝ {Q, D} ∩ ω : Set ℝ²))
    -- Let $Y$ be the point where the circle that touches rays $AB$, $AC$, and is also externally tangent to the circumcircle of $\triangle ABC$, touches the circumcircle of $ \triangle ABC$.
    (Y : ℝ²) (ωY : Sphere ℝ²) (hY :
      ωY.IsTangent (affineSpan ℝ {A, B}) ∧
      ωY.IsTangent (affineSpan ℝ {A, C}) ∧
      ωY.IsExtTangentAt ω Y ∧
      (affineSpan ℝ {A, ωY.center}).SOppSide B C) -- ensure ωY is in the angle BAC
    -- $EF$ is tangent to the incircle of $ \triangle ABC$
    (h : (insphere ⟨![A, B, C], tri⟩).IsTangent (affineSpan ℝ {E, F}))
    -- then $X$, $Y$, and $K$ are collinear.
    : Collinear ℝ {X, Y, K} := by sorry
