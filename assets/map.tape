Output assets/map.gif
Set Shell bash
Set FontFamily "Noto Sans Mono"
Set FontSize 16
Set Width 1100
Set Height 1050
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms
Set PlaybackSpeed 1
Set CursorBlink false

Hide
Type "export PS1='$ '; clear"
Enter
Sleep 500ms
Show
Type "uv run python assets/demo.py"
Enter
Sleep 3s
Screenshot assets/map.png
Type "2"
Enter
Sleep 3s
Screenshot assets/map-detail.png
Type "l"
Enter
Sleep 3s
Screenshot assets/map-limits.png
Type "q"
Enter
Sleep 2s
