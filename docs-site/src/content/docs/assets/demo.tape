# Reble demo: the loop, at a readable pace
# Record: vhs demo.tape
# Output: demo.gif

Output demo.gif
Set FontSize 16
Set Width 1200
Set Height 600
Set TypingSpeed 60ms
Set Shell bash

# --- build production ---
Type@60ms "reble run --refresh"
Enter
Sleep 5s

Type@60ms "# Built both models, in dependency order"
Enter
Sleep 2s

# --- show the model ---
Type@60ms "cat models/stg_orders.sql"
Enter
Sleep 3s

# --- make a change ---
Type@60ms "sed -i '' 's/amount > 10/amount > 20/' models/stg_orders.sql"
Enter
Sleep 2s

# --- scoped rebuild ---
Type@60ms "reble run"
Enter
Sleep 5s

Type@60ms "# Only the changed model + downstream rebuilt"
Enter
Sleep 2s

# --- the diff ---
Type@60ms "reble diff mart_orders"
Enter
Sleep 5s

Type@60ms "# The exact rows your change affects"
Enter
Sleep 2s

# --- status ---
Type@60ms "reble status"
Enter
Sleep 4s

# --- promote ---
Type@60ms "reble promote"
Enter
Sleep 5s

Type@60ms "# No merge. Fast-forward or re-run. That's Reble."
Enter
Sleep 4s
