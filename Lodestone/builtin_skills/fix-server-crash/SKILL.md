---
name: fix-server-crash
description: Find out why a Minecraft server crashed, froze, or will not start, and fix it - from the console, logs/latest.log, crash reports and JVM error files. Use when the user says the server crashed, stopped, hangs, lags out, or fails on startup.
---

# A server that crashed or will not start

## 1. Collect the evidence

- `server_status` and `read_console` (200-400 lines) for the server.
- `logs/latest.log` (read the end of it), the newest file in `crash-reports/`, and any `hs_err_pid*.log` in the server folder (the JVM itself crashed).
- `explain_error` on the first exception: it splits the trace and points at the plugin or mod at fault.

The **first** error is usually the cause; what follows is often fallout.

## 2. Usual causes and what they look like

| What the log says | What it means |
|---|---|
| `UnsupportedClassVersionError ... class file version 65.0` | Needs a newer Java. 52 = Java 8, 55 = 11, 61 = 17, 65 = 21. |
| `**** FAILED TO BIND TO PORT!` / `Address already in use` | Another program (or another server) has the port. Change `server-port` or stop the other one. |
| `You need to agree to the EULA` | `eula.txt` - the user has to accept it themselves (https://aka.ms/MinecraftEULA). |
| `java.lang.OutOfMemoryError` | Not enough memory for the server: raise the max RAM, or find what fills it (too many loaded chunks, a leaking plugin). |
| `Unknown/missing dependency` / `depends on ... which is not installed` | A plugin or mod needs another one. Install it, or remove the plugin. |
| `Mixin apply failed` / `Incompatible mods found` | A mod that does not fit this Minecraft or loader version, or two mods that clash. |
| `The server has stopped responding!` + a thread dump (Watchdog) | The main thread was stuck: read its stack in the dump - the first plugin/mod frames there are the culprit. |
| `Could not pass event ... to <Plugin>` | A plugin's bug; the plugin is named. Update or report it. |
| Chunk / region errors | A corrupted world area; restore a backup of it. |

## 3. Fix, then prove it

1. Make the smallest change that fixes the cause (a config value, a missing dependency, a Java version, removing a broken plugin - say which).
2. `start_server` (or `restart_server`) and read what it returns: it waits for `Done (...)!` or tells you why it stopped.
3. If it fails again, the new first error is the next thing to fix.

Tell the user what the cause was (quote the log line), what you changed, and the startup result. If the fix needs them (accepting the EULA, buying RAM, a plugin author's update), say exactly what.
