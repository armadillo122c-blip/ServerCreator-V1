---
name: skript
description: Write, fix or explain Skript scripts (.sk) for a server running the Skript plugin, checked by Skript itself. Use for anything about Skript commands, events, variables, functions or addons like SkBee.
---

# Skript

Skript's syntax depends on the installed Skript version and addons. Never write syntax from memory alone - check it.

## Steps

1. `skript_environment` - the Skript version, the addons installed, the scripts there and problems already found. Only use addon syntax (SkBee, skript-reflect, ...) if that addon is installed.
2. Read the script you are changing (`read_file plugins/Skript/scripts/<name>.sk`) before editing it.
3. Write the change.
4. `skript_reload` the script - this is Skript's own parser answering. Fix every error it reports (it gives the line and the reason) and reload again until it reloads cleanly.
5. If the server is not running, `check_file` gives the structure checks (indentation, colons, sections), but say that Skript itself has not checked it yet.

## Things that trip people up

- Indentation is the structure: every line inside a section is indented one level more than the line that opened it, the same way throughout a file (tabs or the same number of spaces).
- A line that opens a section ends with `:` (`command /shop:`, `trigger:`, `on join:`, `if ...:`).
- Variables: `{name}` is global and saved, `{_name}` is local to the trigger, `{list::*}` is a list; per-player data usually uses the UUID: `{coins::%player's uuid%}`.
- Functions: `function give_kit(p: player):` then call `give_kit(player)`; a function that returns something declares it: `function double(n: number) :: number:` and `return {_n} * 2`.
- `options:` at the top defines `{@name}` replacements.
- Every loop over all players or entities costs tick time - avoid `every tick` loops over everything; use events instead.
- Files whose name starts with `-` are disabled.

## When unsure about a piece of syntax

Look it up rather than guess: the Skript documentation at https://docs.skriptlang.org/ (use `fetch_url` if the web is on), or addon docs on https://skripthub.net/docs/. Then let `skript_reload` confirm it.

Finish with what changed (file:line), and the reload result.
