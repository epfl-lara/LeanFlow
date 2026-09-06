import Mathlib

/-
On a table of size $3002\times3001$, a stone is placed on the leftmost cell of the first row. James and Peter play a game on this table. Peter selects $3000$ cells, under the rule that he must choose one from each row except the first and last rows (i.e., the $1$st and $3002$th row), and there must be at most one selected cell in each column. James knows this rule too, but he doesn't know which cells Peter selected. The goal of James is to move the stone to the last row, avoiding the cells selected by Peter. The stone can only move to adjacent cells on the table. If the stone enters a cell selected by Peter, James receives a penalty of 1 point, and the stone returns to its initial position (i.e., the leftmost cell). Find the smallest positive integer $n$ such that there exists a method for James to achieve his goal before receiving a penalty of $n$ points.

Answer: 3
-/
open Classical

-- Game Constants
def R_rows : ℕ := 3002
def C_cols : ℕ := 3001
abbrev Cell : Type := ℕ × ℕ

-- Location definitions
/-- A cell is on the table if its indices are within [1, R_rows] x [1, C_cols]. -/
def IsOnTable (c : Cell) : Prop :=
  c.fst > 0 ∧ c.fst ≤ R_rows ∧ c.snd > 0 ∧ c.snd ≤ C_cols

def start_cell : Cell := (1, 1)
/-- The goal is to reach any cell in the last row. -/
def target_row : Set Cell := { (r, c) | r = R_rows ∧ IsOnTable (r, c) }

-- Adjacency: orthogonal movement within bounds
/-- A move from c₁ to c₂ is adjacent if they are orthogoanlly next to each other and c₂ is on the table. -/
def IsAdjacentMove (c₁ c₂ : Cell) : Prop :=
  IsOnTable c₂ ∧
  (abs ((c₁.fst : ℤ) - (c₂.fst : ℤ)) + abs ((c₁.snd : ℤ) - (c₂.snd : ℤ)) = 1)

-- Peter's Constraints
/-- The set of rows Peter must choose one blocking cell from: {2, 3, ..., R_rows - 1}. -/
def PeterRows : Set ℕ := {r : ℕ | 2 ≤ r ∧ r ≤ R_rows - 1}
/-- The set of valid columns. -/
def Columns : Set ℕ := {c : ℕ | 1 ≤ c ∧ c ≤ C_cols}

/-- A set of selected cells S that satisfies Peter's rules: one cell per row \{2, ..., R-1\}, at most one cell per column {1, ..., C}. -/
def IsPeterSelection (S : Set Cell) : Prop :=
  -- 1. Cells are valid and within Peter's designated rows/columns
  (∀ (r c : ℕ), (r, c) ∈ S → r ∈ PeterRows ∧ c ∈ Columns) ∧
  -- 2. Exactly one selected cell per Peter row
  (∀ r ∈ PeterRows, ∃! c : ℕ, (r, c) ∈ S) ∧
  -- 3. At most one selected cell per column (injectivity)
  (∀ (r₁ c₁ r₂ c₂ : ℕ), (r₁, c₁) ∈ S → (r₂, c₂) ∈ S → c₁ = c₂ → r₁ = r₂)

def PeterSelections : Set (Set Cell) := {S : Set Cell | IsPeterSelection S}

/--
A valid path is a list of cells that starts at the start cell,
finishes at the target row, and consists of adjacent, on-table moves. -/
def IsValidPath (P : List Cell) : Prop :=
  P.head? = some start_cell ∧
  (∃ c ∈ target_row, P.getLast? = some c) ∧
  (∀ c : Cell, c ∈ P → IsOnTable c) ∧
  P.Chain' IsAdjacentMove

/-- A path is winning against a selection S if it is valid, avoids S entirely, and ends on the target row. -/
noncomputable
def pathFirstHit (P : List Cell) (S : Set Cell) : Option (Fin P.length) :=
  P.findFinIdx? (fun c ↦ c ∈ S)

def PathIsWinning (P : List Cell) (S : Set Cell) : Prop :=
  IsValidPath P ∧
  (∀ c : Cell, c ∈ P → c ∉ S)

/--
James' strategy is a finite tree of choosing paths.
Every time James tries a path, this path either succeeds, or James gets an information about
at which step it failed. This equals to the maximum possible penalty a strategy can get.
-/
inductive JamesStrategy
| giveUp
| tryPath (P : List Cell) (hP : IsValidPath P)
  (next : (i : ℕ) → i < P.length → JamesStrategy)

/-- The maximal number of steps a strategy can take -/
noncomputable
def JamesStrategy.depth : JamesStrategy → ℕ
| giveUp => 0
| tryPath P _ next => sSup { (next i.val i.isLt).depth | (i : Fin P.length) } + 1

def JamesStrategy.winsOn (S : Set Cell) : JamesStrategy → Prop
| giveUp => False
| tryPath P _ next =>
  match pathFirstHit P S with
  | some i => (next i.val i.isLt).winsOn S
  | none => True

/-- A given strategy always wins regardless of Peter's selection of cells. -/
def JamesStrategy.wins (strategy : JamesStrategy) : Prop :=
  ∀ S ∈ PeterSelections, strategy.winsOn S

/-- The property that there exists a strategy of length n that guarantees a win before n penalties. -/
def HasWinningStrategyBeforeNPenalties (n : ℕ) : Prop :=
  n > 0 ∧
  ∃ strategy : JamesStrategy, strategy.wins ∧ strategy.depth ≤ n

/-- The smallest positive integer n such that there exists a method for James to achieve his goal before receiving a penalty of n points. -/
noncomputable
def SmallestWinningPenaltyBound : ℕ :=
  sInf {n : ℕ | HasWinningStrategyBeforeNPenalties n}

-- Formalization of the Claim provided in the problem statement
theorem PBAdvanced023 : SmallestWinningPenaltyBound = 3 := by sorry
