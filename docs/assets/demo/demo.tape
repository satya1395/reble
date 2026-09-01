Output demo.gif

Set Shell zsh
Set FontSize 15
Set Width 1120
Set Height 780
Set Padding 16
Set TypingSpeed 35ms
Set Theme { "name": "reble", "background": "#15181e", "foreground": "#d6dae2", "cursor": "#4dd0e1", "black": "#15181e", "red": "#ef7a70", "green": "#66d17a", "yellow": "#e5c07b", "blue": "#61afef", "magenta": "#c678dd", "cyan": "#4dd0e1", "white": "#d6dae2", "brightBlack": "#7c8494", "brightRed": "#ef7a70", "brightGreen": "#66d17a", "brightYellow": "#e5c07b", "brightBlue": "#61afef", "brightMagenta": "#c678dd", "brightCyan": "#4dd0e1", "brightWhite": "#eceff4", "selectionBackground": "#2c3444" }

Hide
Type "setopt interactive_comments && clear"
Enter
Sleep 1s
Show

Sleep 800ms
Type "# finance: revenue is wrong. the fix is one line in stg_orders:"
Enter
Type "tail -3 models/core/stg_orders.sql"
Enter
Sleep 2.5s

Type "git switch -c fix-cancelled-revenue"
Enter
Sleep 1.5s

Type "reble run"
Enter
Sleep 4s

Type "reble status"
Enter
Sleep 3.5s

Type "reble diff"
Enter
Sleep 5s

Type `reble query "SELECT * FROM core.fct_revenue_daily ORDER BY revenue DESC LIMIT 5"`
Enter
Sleep 5s

Type "reble promote"
Enter
Sleep 5s
