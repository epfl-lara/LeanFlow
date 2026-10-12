import Mathlib

/-
Alice the architect and Bob the builder play a game. First, Alice
chooses two points $P$ and $Q$ in the plane and a subset $S$ of
the plane, which are announced to Bob. Next, Bob marks infinitely
many points in the plane, designating each a city. He may not place
two cities within distance at most one unit of each other, and no
three cities he places may be collinear. Finally, roads are constructed
between the cities as follows: for each pair $A,B$ of cities, they
are connected with a road along the line segment $AB$ if and only
if the following condition holds: For every city $C$ distinct from
$A$ and $B$, there exists $R\in S$ such that $\triangle PQR$ is
directly similar to either $\triangle ABC$ or $\triangle BAC$. Alice
wins the game if

\noindent (i) the resulting roads allow for travel between any pair
of cities via a finite sequence of roads and

\noindent (ii) no two roads cross.

\noindent Otherwise, Bob wins. Determine, with proof, which player
has a winning strategy. (Note: $\triangle UVW$ is directly similar
to $\triangle XYZ$ if there exists a sequence of rotations, translations,
and dilations sending $U$ to $X$, $V$ to $Y$, and $W$ to $Z$.)

Answer: Alice
-/

open EuclideanGeometry Relation
local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)

def rot (P : ℝ²) : ℝ² := (WithLp.equiv 2 _).symm ![-P 1, P 0]

def DirectlySimilar (A B C A' B' C' : ℝ²) : Prop :=
  ∃ (a b : ℝ) (v : ℝ²), (a ≠ 0 ∨ b ≠ 0) ∧
    A' = a • A - b • rot A + v ∧
    B' = a • B - b • rot B + v ∧
    C' = a • C - b • rot C + v

-- Alice chooses two points $P$ and $Q$ in the plane and a subset $S$ of the plane
structure AliceData where
  (P Q : ℝ²)
  S : Set ℝ²

/-
Bob marks infinitely
many points in the plane, designating each a city. He may not place
two cities within distance at most one unit of each other, and no
three cities he places may be collinear.
-/
def ValidBobSet (S : Set ℝ²) : Prop :=
  S.Infinite ∧
  (∀ A ∈ S, ∀ B ∈ S, A ≠ B → dist A B > 1) ∧
  (∀ triple ⊆ S, triple.encard = 3 → ¬ Collinear ℝ triple)

/-
For each pair $A,B$ of cities, they are connected with a road along
the line segment $AB$ if and only if the following condition holds:
For every city $C$ distinct from $A$ and $B$, there exists $R\in S$
such that $\triangle PQR$ is directly similar to either $\triangle ABC$
or $\triangle BAC$.
-/
def hasRoad (alice : AliceData) (bob : Set ℝ²) (A B : ℝ²) : Prop :=
  ∀ C ∈ bob, C ≠ A → C ≠ B →
  ∃ R ∈ alice.S,
    DirectlySimilar A B C alice.P alice.Q R ∨
    DirectlySimilar B A C alice.P alice.Q R

-- Alice wins the game if
def AliceWins (alice : AliceData) (bob : Set ℝ²) : Prop :=
  /-
  (i) the resulting roads allow for travel between any pair
  of cities via a finite sequence of roads and
  -/
  (∀ A ∈ bob, ∀ B ∈ bob, ReflTransGen (hasRoad alice bob) A B) ∧
  -- (ii) no two roads cross.
  (∀ A ∈ bob, ∀ B ∈ bob, ∀ C ∈ bob, ∀ D ∈ bob,
    [A, B, C, D].Nodup → hasRoad alice bob A B → hasRoad alice bob C D →
    (openSegment ℝ A B) ∩ (openSegment ℝ C D) = ∅)

-- Proposition that Alice can win the game no matter of Bob's action
def AliceHasWinningStrategy : Prop :=
  ∃ alice : AliceData, ∀ bob : Set ℝ², ValidBobSet bob → AliceWins alice bob

-- We want to prove that Alice has a winning strategy
theorem PBAdvanced027 : AliceHasWinningStrategy := by sorry
