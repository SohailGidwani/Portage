# VHS tape for docs/assets/kill-resume.gif
#
#   vhs scripts/kill-resume.tape
#
# Records scripts/demo_kill_resume.sh: a real migration, a real SIGKILL of the worker
# mid-run, and the resume from the Postgres checkpoint — ending on the CLI's verdict
# panel (outcome · full suite · oracle integrity · recovery · cost).
#
# Needs: the compose stack up, the `portage` CLI on PATH, and an API with
# AUTH_MODE=disabled reachable at $PORTAGE_API.

Output "/private/tmp/claude-501/-Users-sohail-Documents-Portage/e86383f7-9ae2-4129-a501-ba7d052226b2/scratchpad/kr.mp4"

Set Shell "bash"
Set FontSize 18
Set Width 1500
Set Height 1000
Set Padding 30
Set Theme "Dracula"
Set WindowBar Colorful
Set WindowBarSize 40
Set BorderRadius 12
Set TypingSpeed 30ms
Set Margin 26
Set MarginFill "#0f1420"

Hide
Type "cd /Users/sohail/Documents/Portage && source apps/backend/.venv/bin/activate && export PORTAGE_API=http://localhost:8001 && clear" Enter
Sleep 1500ms
Show

Type "bash scripts/demo_kill_resume.sh" Sleep 500ms Enter
Sleep 100s
Sleep 3s
