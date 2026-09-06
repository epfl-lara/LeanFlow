import Mathlib

/-
Let $\triangle ABC$ be an inscribed triangle in $(O)$ and circumscribed
around $(I)$. The incircle $(I)$ touches $BC,CA,AB$ at $D,E,F$,
respectively. Construct the circle $(W_{a})$ passing through $B,C$
and tangent to $(I)$ at $X$, and let $D'$ be the reflection of
$D$ across $AI$. Define $Y,Z,E',F'$ similarly. Prove that the lines
$D'X,E'Y,F'Z$ are concurrent on the line $OI$.
-/
open Real Affine Simplex EuclideanGeometry
local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)

-- We start by defining the auxiliary construction.
-- Given points A, B, C, we define a predicate that a given line
-- was constructed as the line `D'X`.
def isLineDX (A B C : ℝ²) (dx : AffineSubspace ℝ ℝ²) : Prop :=
  ∃ (tri : AffineIndependent ℝ ![A, B, C]) (W : Sphere ℝ²) (X : ℝ²),
  B ∈ W ∧ C ∈ W ∧
  let I := incenter ⟨![A, B, C], tri⟩
  let D := touchpoint ⟨![A, B, C], tri⟩ ∅ 0
  (insphere ⟨![A, B, C], tri⟩).IsIntTangentAt W X ∧
  let D' := reflection (affineSpan ℝ {A, I}) D
  dx = affineSpan ℝ {D', X}

-- The main problem does this construction 3-times, and claims the result
-- is on the line O I
theorem PBBasic026
    (A B C : ℝ²) (tri : AffineIndependent ℝ ![A, B, C])
    (l1 l2 l3 : AffineSubspace ℝ ℝ²) :
    isLineDX A B C l1 →
    isLineDX B C A l2 →
    isLineDX C A B l3 →
    let O := circumcenter ⟨![A, B, C], tri⟩
    let I := incenter ⟨![A, B, C], tri⟩
    ∃ p : ℝ², p ∈ l1 ∧ p ∈ l2 ∧ p ∈ l3 ∧ p ∈ affineSpan ℝ {O, I} := by sorry
