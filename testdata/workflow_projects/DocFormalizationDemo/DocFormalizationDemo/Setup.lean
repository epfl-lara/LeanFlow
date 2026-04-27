import Mathlib

/-!
# Setup for document formalization smoke tests

This file keeps the demo project buildable before EPFLemma generates any
document-specific formalization target.  The source document in
`docs/Hamming74SingleErrorCorrection.tex` points at these coding-theory
definitions as local context that a planner can reuse.
-/

namespace DocFormalizationDemo

abbrev Bit :=
  ZMod 2

abbrev Word (n : Nat) :=
  Fin n -> Bit

def hammingWeight {n : Nat} (x : Word n) : Nat :=
  Fintype.card { i : Fin n // x i ≠ 0 }

def hammingDistance {n : Nat} (x y : Word n) : Nat :=
  Fintype.card { i : Fin n // x i ≠ y i }

def hamming74Encode (m : Word 4) : Word 7 :=
  ![
    m 0 + m 1 + m 3,
    m 0 + m 2 + m 3,
    m 0,
    m 1 + m 2 + m 3,
    m 1,
    m 2,
    m 3
  ]

def hamming74Syndrome (w : Word 7) : Word 3 :=
  ![
    w 0 + w 2 + w 4 + w 6,
    w 1 + w 2 + w 5 + w 6,
    w 3 + w 4 + w 5 + w 6
  ]

def flipAt {n : Nat} (w : Word n) (i : Fin n) : Word n :=
  fun j => if j = i then w j + 1 else w j

end DocFormalizationDemo
