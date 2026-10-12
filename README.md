# Server Creator AI

Create, run, develop and tune Minecraft servers - with an AI assistant that runs
on your own computer.

This is **Lodestone** (the app in [`Lodestone/`](Lodestone/)) grown into a
server development environment. Everything it already did stays: the create
wizard that really installs Vanilla, Paper, Purpur, Spigot, Fabric, Quilt, Forge
and NeoForge with a Java that fits; the mod and plugin browser (Modrinth, Hangar,
SpigotMC) with compatibility checks, dependency resolution, hash-verified
downloads, updates and removal; the console, files, properties, backups,
automation, RCON, Discord, internet hosting and the REST API. On top of it:

| | |
|---|---|
| **AI Assistant** | A local model ([Ollama](https://ollama.com)) working inside a server's folder or a plugin/mod project: it reads the whole project, edits files, runs the build, reloads Skript and checks its own work. Every file change is shown as a diff and every command as its exact arguments before anything happens; changes can be put back. Models with tool calling use Ollama's native tool calls; others get a JSON fallback. |
| **Code** | An editor with checks for YAML, JSON, TOML and `server.properties`; Skript analysed with Skript's own reader rules and verified by Skript itself on the running server; Gradle/Maven projects with their usual mistakes, builds with clickable errors; stack traces explained from the trace itself. |
| **Performance** | The server's CPU and memory (its whole process tree), TPS and MSPT only as the server reports them, host memory/CPU/GPU, Ollama's loaded models - and a **Development Performance Mode** for coding with a local model on the same machine. |

Nothing here costs money or needs an account: Ollama and its models are free and
run locally; Modrinth, Hangar, Mojang, PaperMC, Fabric, Forge, NeoForge and
Adoptium are used through their free public endpoints, as before.

## Getting started

On Windows, double-click **`Lodestone/Setup Lodestone.bat`** once - it checks for
Python and the .NET SDK (offering to install them with winget), builds the
desktop app and makes shortcuts. Details, other platforms and building by hand
are in [`Lodestone/README.md`](Lodestone/README.md).

For the AI Assistant:

1. Install Ollama from <https://ollama.com/download> and start it.
2. Pull a model that supports tool calling (they are marked *tools* at
   <https://ollama.com/search?c=tool>), for example
   `ollama pull qwen2.5-coder:7b`.
3. In the app: **AI Assistant → Model & resources**, pick the model, save.
4. Pick a workspace - a server, or **+** to add a plugin/mod project folder - and
   describe what to build or fix.

## Status

Built and tested on Linux (Python 3.13, .NET 8): `py Lodestone/tests/run_all.py`
runs every suite, including the new ones - the assistant against a fake Ollama
that speaks Ollama's documented wire format, the checks against real plugin
configs and Skript's own example scripts, and the performance numbers against
real processes. The desktop pages were checked with the app's headless
screenshot mode (`--shots`).

Not verified here, because this environment has neither: the Windows-only
measurement code in `perfstats.py` (written against the Win32 API, untested on
Windows), and a real Ollama model - the wire format comes from Ollama's API
reference, but how well a given model plans and uses the tools is up to the
model. Two older suites (`test_networking.py`, `test_ram.py`) have checks that
only pass on Windows.
