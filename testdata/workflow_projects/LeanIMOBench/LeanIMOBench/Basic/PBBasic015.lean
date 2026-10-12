import Mathlib

-- Adrian is lining up $n \geq 2$ toy cars in a row. Each car has a width and length, and no two cars have the same width or length. Initially, the cars are arranged in increasing order of length from left to right. Adrian repeatedly does the following: he chooses some two adjacent cars where the left car is shorter in length and greater in width than the right car, and he swaps them. He keeps doing this until no further moves are possible. Prove that no matter how Adrian chooses his swaps, the procedure will eventually terminate, and in the end, the cars will be sorted in increasing order of width from left to right.

structure Car where (width length : Nat)

def Cars (n : ℕ) := Fin n → Car

variable {n : ℕ}

-- 1. Setup: Initial Conditions
structure Cars.IniCond (cars : Cars n) : Prop where
  distinct_width : (Car.width ∘ cars).Injective
  distinct_length : (Car.length ∘ cars).Injective
  increasing_length : Monotone (Car.length ∘ cars)

-- 2. Setup: The Swap Move
structure Cars.Swap (cars : Cars n) where
  (i1 i2 : Fin n)
  adjacent : i1.val + 1 = i2.val
  shorter_length : (cars i1).length < (cars i2).length
  greater_width : (cars i1).width > (cars i2).width

def Cars.Swap.apply {cars : Cars n} (step : Swap cars) : Cars n :=
  cars ∘ Equiv.swap step.i1 step.i2

-- The relation: `next` is reachable from `prev` via one swap
def Cars.swapRel (prev next : Cars n) : Prop :=
  ∃ step : prev.Swap, step.apply = next

-- 3. The Combined Theorem
theorem PBBasic015
  (start : Cars n)
  (ini : start.IniCond) :
  -- Part 1: Termination (No infinite chain of swaps starts from `start`)
  (Acc (fun next prev => Cars.swapRel prev next) start) ∧
  -- Part 2: Correctness (Any reachable state with no moves left is sorted)
  (∀ final, Relation.ReflTransGen Cars.swapRel start final →
    IsEmpty final.Swap → Monotone (Car.width ∘ final)) := by sorry
