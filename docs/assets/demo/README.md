# Recording the README demo GIF

The GIF shows README scenario 1 ("finance says revenue is wrong") on the real
tool — nothing is mocked.

```bash
# stage the 1.2M-row hero warehouse (leaves the fix applied, ready to branch)
zsh stage.sh /tmp/gifdemo

# record (needs `brew install vhs`); run the tape from inside the staged project
cp demo.tape /tmp/gifdemo/ && cd /tmp/gifdemo && vhs demo.tape
cp demo.gif <repo>/docs/assets/demo.gif
```

The tape's theme matches the CLI design palette in `../cli-design.html`.
