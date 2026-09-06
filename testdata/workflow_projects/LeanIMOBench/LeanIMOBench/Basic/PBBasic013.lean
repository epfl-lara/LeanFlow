import Mathlib

-- Each of 8 boxes contains 6 balls. Each ball has been colored with one of 22 colors. If no two balls in the same box are the same color, prove that there are two colors that occur together in more than one box.

theorem PBBasic013 (Box Color : Type)
    -- there are 8 boxes and 22 colors
    (num_boxes : Box ≃ Fin 8) (num_colors : Color ≃ Fin 22)
    -- each box contains 6 distinct balls
    (balls : Box → (Finset Color)) (num_balls : ∀ box : Box, (balls box).card = 6) :
    -- then there exist two colors and two boxes, both containing both of the two colors
    ∃ (color1 color2 : Color) (box1 box2 : Box), color1 ≠ color2 ∧ box1 ≠ box2 ∧
    color1 ∈ balls box1 ∧ color2 ∈ balls box1 ∧ color1 ∈ balls box2 ∧ color2 ∈ balls box2 := by sorry
