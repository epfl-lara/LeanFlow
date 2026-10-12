import Mathlib

/-
In $\triangle ABC$ the altitudes $BE$ and $CF$ intersect at $H$. A circle $(W)$ is
externally tangent to the Euler circle $(E)$ of $\triangle ABC$ and also tangent
to the sides $AB$ and $AC$ at $X$ and $Y$, respectively, with
the center of $(W)$ inside $\triangle ABC$. Let $I'$ be the
incenter of $\triangle AEF$. Prove that $AXI'Y$ is a rhombus.
-/
open Real Affine Simplex EuclideanGeometry
local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)

theorem PBBasic028
    (A B C : ℝ²) (tri : AffineIndependent ℝ ![A, B, C])
    (ω : Sphere ℝ²) -- ω is the 9-point (Euler) circle
    (hω : midpoint ℝ A B ∈ ω ∧ midpoint ℝ B C ∈ ω ∧ midpoint ℝ C A ∈ ω)
    (W : Sphere ℝ²) (X Y : ℝ²) -- W is a circle that
    (hX : W.IsTangentAt X (affineSpan ℝ {A, B})) -- is tangent to AB at X
    (hY : W.IsTangentAt Y (affineSpan ℝ {A, C})) -- is tangent to AC at Y
    (tangent : W.IsExtTangent ω) -- is externally tangent to the 9-point circle
    (inside : W.center ∈ convexHull ℝ {A, B, C}) : -- and the center of W is inside triangle ABC
    let E := altitudeFoot ⟨![A, B, C], tri⟩ 1 -- E is the foot from B
    let F := altitudeFoot ⟨![A, B, C], tri⟩ 2 -- F is the foot from C
    let H := Triangle.orthocenter ⟨![A, B, C], tri⟩ -- H is the orthocenter
    ∀ tri2 : AffineIndependent ℝ ![A, E, F], -- aux assumption to take the incenter
    let I' := incenter ⟨![A, E, F], tri2⟩ -- I' is the incenter of AEF
    -- then all `dist A X`, `dist X I'`, `dist I' Y`, `dist Y A` are equal
    List.Pairwise (· = ·) [dist A X, dist X I', dist I' Y, dist Y A] := by sorry
