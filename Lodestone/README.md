# Lodestone

One panel for every Minecraft server you run — the JAR on this machine and the
box you only reach over SFTP, in the same window.

This is LocalHost and RemoteHost merged. Instead of two apps that each did half
the job, there is one transport-agnostic panel: files, properties, backups,
console and automation work identically whether the server is a process on this
PC or a folder on a host somewhere. On top of that merge it adds RCON, a Discord
event router, a token-authenticated REST API, and **internet hosting** — one
switch that opens the Minecraft port on your router and then proves, from
outside, whether friends can actually reach you.

**Lodestone is a native desktop app now** — C# and Avalonia, styled after
Modrinth, in `desktop/`. It is a real window, not a browser page, and it builds
for Windows, Linux and macOS. The Python engine (`lodestone.py`) still does the
work underneath; the app starts it with `--headless` and drives it over its local
API. See [The desktop app](#the-desktop-app).

**Server Creator AI.** On top of running servers, Lodestone is a development
environment for them: an **AI Assistant** that runs on your own computer with
[Ollama](https://ollama.com) and works inside a server's folder or a
plugin/mod project - reading the whole project, editing files, running the build
and checking its own work, with every change and command shown to you first; a
**Code** page with an editor and tools for Skript, Java, configs and stack
traces; and a **Performance** page that shows only numbers the operating system
or the server itself reported. Everything is free and local. See
[AI Assistant](#ai-assistant), [Code](#code) and [Performance](#performance).

**Just want to run it?** On Windows, double-click **`Setup Lodestone.bat`** once.
It checks for Python and the .NET SDK (and offers to install whichever is
missing with winget), builds the desktop app, puts Lodestone on your Desktop and
in the Start menu, and starts it. After that, use the shortcut - or
`Lodestone.vbs`, which builds the window by itself on its first start if the
.NET SDK is there, and falls back to the browser page if it is not.

From source, the classic browser page:

```bash
py lodestone.py
```

Opens at <http://127.0.0.1:8744>. Standard library only for local servers;
remote servers need `paramiko` (Settings has a one-click installer).

It opens on your **Instances**: every server you have, as a card. Click one to
step inside it. With no servers yet you get a welcome screen with three big
choices instead - create a new server, add one you already have, or connect to
a remote one - and nothing to answer first.

Someone new starts in **Simple**; someone who already has servers keeps
everything. The welcome screen and Settings switch between the two:

* **Simple** — console, files, properties, backups, mods & plugins, instance settings. Everything
  needed to run a server and nothing else.
* **Advanced** — all of that plus log search, plugins and worlds, automation, the
  AI assistant, Discord webhooks and the REST API.

Nothing is deleted either way; Simple only hides tabs. Switch any time by
clicking the word next to the Lodestone logo, or in Settings.

---

## What it does

| | Local server | Remote server (SFTP) |
|---|---|---|
| Start / stop / restart | yes, panel owns the process | yes, on Minehut |
| Console | live process output | live tail of `logs/latest.log` |
| Send commands | stdin, or RCON | RCON, or the Minehut API |
| Files, properties, backups | yes | yes |
| Players, moderation | yes | with RCON on |
| Worlds, MOTD, icon, plugins, log search | yes | yes |
| Assistant, Discord, automation, API | yes | yes |
| Local / LAN / internet hosting | yes | n/a — the host owns the port |

### Instances: pick a server, then work inside it
Lodestone works the way a game launcher does. **Home is your library** - every
server as a card with its banner, icon, software and port. Click a card (or
**Open**) to step inside that server; right-click it for everything else: Play
or Stop, Instance settings, Change icon, Duplicate, profiles, Remove.

**Inside an instance** the sidebar becomes that server's own tabs - **Server**
(console, files, properties, backups), **Manage** (mods & plugins, instance
settings, tools) and **Automate** (automation, the assistant, Discord) - under a
card naming it; click the card to switch to another server, or **All
instances** to step back out. The title bar says where you are, and every step
of it is a way back: `Instances › My server ▾ › Console`. At home, the sidebar
lists the servers to **jump back into**, running ones first.

**Instance settings** is the server's own setup - name, folder, jar, Java,
memory, the SFTP login of a remote one, RCON, its banner, icon and profile.
Game rules like the MOTD and difficulty stay in Properties.

A **local server is running or stopped**; only a remote one is "connected".

### Instance icons: Minecraft's items and blocks
Every instance's icon is a **Minecraft item or block** - about a thousand to
pick from, in groups (Blocks, Ores & minerals, **Redstone & tech**, Workstations,
Tools & combat, Armor, Food, Magic & rare, Mobs, Music, Items) with a search box
and a **Surprise me**. Until you pick one, the server gets one from what it runs:
a grass block for Vanilla, paper for Paper, a purpur block for Purpur, a water
bucket for Spigot, a loom for Fabric, a smithing table for Forge, a blast
furnace for NeoForge - and a lodestone for anything else.

Lodestone does not ship Mojang's textures. The first time it starts it reads
them out of **the official game download** on Mojang's servers, the client jar
every launcher fetches - and not all 40 MB of it: a jar is a zip with its index
at the end, and Mojang's CDN answers range requests, so it reads the index and
then just the stretch where the textures sit, about half a megabyte in four
requests. They are kept in `~/.lodestone/textures`. Blocks are drawn as cubes
in the inventory's three-quarter view, slabs and short blocks at their own
height; items are their sprite, scaled by a whole number so pixels stay square.

### Adding a server you already have
**Add existing → A server folder on this PC**, pick the folder, and Lodestone
works out the rest: the server jar (the launcher, not the installer next to it),
the software and Minecraft version (from the jar's own version file, Paper's
history file, or the folder's config files), the port, whether there is a world
and an accepted EULA, a Java that fits, and memory for this PC. Modern Forge and
NeoForge have no server jar: their args file is found instead, and their
`user_jvm_args.txt` is respected the way Forge's own `run.bat` does it. A folder
already in the library just opens; an empty one offers to create a server there.

A remote server asks for its login up front, tests the connection, and finds
the server folder on the host if `/` was a guess.

### The library
The library can be searched, cut down to running, local or remote servers or a
profile, and sorted by status, name, last start or profile, as cards or a list.

### Creating a server
**Create server** on the Servers tab is a four-step wizard that downloads and
installs a new server, then puts it on the shelf like any other — start, stop,
console, files, properties and logs all work on it straight away.

1. **Software.** Pick a Minecraft version, then the software. Only what
   actually publishes a build for that version can be picked: every service is
   asked, and the list is the intersection.
2. **Mods or plugins.** A browser for exactly what this server will load (see
   below), plus a one-click recommended set. Dependencies are worked out for the
   exact version being created and shown before anything is downloaded.
3. **Settings.** Name, folder, port, memory, Java, and the usual
   `server.properties` choices — MOTD, difficulty, game mode, max players, view
   distance, seed, PvP, whitelist, online mode.
4. **Create.** A live progress view with each step, a percentage and the full
   log. If anything fails, everything the job made is removed again and the
   reason is shown in a sentence, so changing one setting and trying again is
   safe.

| Software | Where it comes from | How it is installed |
|---|---|---|
| **Vanilla** | Mojang's version manifest | the server jar, sha1-verified |
| **Paper** | PaperMC's Fill API (v3) | the jar, sha256-verified |
| **Purpur** | Purpur's API | the jar, md5-verified |
| **Spigot** | not published as a download | BuildTools compiles it on this PC — several minutes |
| **Fabric** | Fabric meta | the server launcher, plus the verified vanilla jar it would fetch on first start |
| **Quilt** | Quilt meta | the official installer is run, verified against the maven's sha1 |
| **Forge** | Forge's maven | the official installer is run; modern Forge launches from its `win_args.txt` |
| **NeoForge** | NeoForged's maven | the official installer is run, same as Forge |

**Java is handled too.** The Java a version needs is read from Mojang's own
version file (26.x needs 25, 1.20.5–1.21 need 21, 1.18–1.20.4 need 17, older
needs 8). Installed Javas are found — `JAVA_HOME`, `PATH`, the usual install
folders, even the Minecraft launcher's bundled runtimes — and asked their
version. If none fits, an Eclipse Temurin runtime is downloaded from Adoptium
into `~/.lodestone/java/` and checked against its published sha256. Nothing is
installed system-wide and `PATH` is never touched. Forge and NeoForge always
get the exact Java their version shipped with, because mod loaders are the
first thing to break on a newer one.

It will not accept the Minecraft EULA for you. The wizard has a switch for it;
leave it off and the server is created but will not start until you accept.

### Mods & plugins
A tab of its own: **Installed** and **Browse**. A server gets mods *or* plugins,
never both, decided by what it runs — a Fabric server is never offered a Bukkit
plugin, and a Paper server is never offered a Fabric mod.

| | Mods (Fabric, Quilt, Forge, NeoForge) | Plugins (Paper, Purpur, Spigot) |
|---|---|---|
| **Modrinth** | yes — filtered by loader, version, and not client-only | yes — filtered by loader and version |
| **Hangar** | — | Paper and Purpur (Hangar builds are Paper plugins) |
| **SpigotMC** | — | yes, through the Spiget mirror |

Each source is checked as far as it can be. Modrinth says exactly which loader
and game version every build is for, so a mod without a build for this server
is refused with the reason. Hangar says which versions a build supports.
SpigotMC only has an author's "tested versions" list, which routinely stops
years short of what a plugin really runs on (Vault), so there it is a warning
rather than a refusal — and premium or externally hosted resources are named,
not fetched.

**Dependencies are installed with the thing that needs them**, at a version
that fits, and the install dialog shows the whole list first: what you asked
for, what comes with it and why, what will not be installed and why, and
optional extras worth knowing about. A dependency hosted outside Hangar is named
with its link rather than silently missing.

Everything is downloaded to a temporary file, checked against the published
hash, and only then put in place. What was installed, from where, and what it
was pulled in for is recorded in `<server>/.lodestone/content.json`, which is
what makes the rest sensible:

* **Updates** check every installed file against its source and replace exactly
  the jar the old version put there. The config folder stays.
* **Remove** says which other installs need the one being removed.
* **Disable** renames to `.jar.disabled`, the way server owners already do it.
* **Jars put there by hand** are identified by asking Modrinth about their hash,
  and every jar's own metadata (`fabric.mod.json`, `mods.toml`, `plugin.yml`, …)
  is read — so a Forge mod dropped into a Fabric server is flagged as one that
  will not load, and a mod whose dependency was removed says so.

Servers added by hand work too: what they run is detected from their folder,
the same way the Via installer does it.

The installed list opens with the numbers — how many you installed, how many
came in as dependencies, how many need attention, updates waiting, space used —
then groups everything by what deserves a look first: **needs attention**,
**installed by you**, **dependencies**, **disabled**. It can be searched, filtered
by any of those, and sorted by name, install date, size or source. Every entry
shows its project's icon, or for a jar nobody indexed, the icon packed inside the
jar itself. Browsing has category chips (Modrinth's and Hangar's own), and the
install dialog shows the project's screenshots, featured first.

### Updates
Lodestone keeps itself up to date from its GitHub repository - by default
`armadillo122c-blip/ServerCreator-V1`, branch `main`, folder `Lodestone/`. A
config that still names the repository Lodestone used to live in
(`armadillo122c-blip/CLAUDE`) is pointed here, since updating from there would
replace this version with the older one. **Settings ->
Updates** shows the installed version, what is new (the commit list), and the
switches.

* **Checking** happens a little after Lodestone starts and then every few hours
  (1 hour to a week). Only commits that touch the Lodestone folder count.
* **Installing** happens by itself (switchable): the commit is downloaded as a
  zip, only the Lodestone folder is taken out of it, and every Python file must
  compile before anything is touched. Files are swapped one at a time with the
  old ones copied aside first; if one cannot be replaced, everything is put back.
  `dist/`, `bin/`, `obj/`, logs and backups are never touched, and a file is only
  deleted if a previous update put it there - your own files stay.
* **Restarting** is a button (**Restart now**), or automatic while no server is
  running if you switch that on. Lodestone quits, `updater.py --relaunch` waits
  for it to close, rebuilds the desktop window if its source changed
  (`desktop/build.py --dll`, the same as by hand), and starts Lodestone again.
  If the new version does not answer within a minute and a half, the previous
  one is put back and started instead. Restarting the normal way works too:
  `Lodestone.vbs` rebuilds the window first when an update asked for it.
* **Going back** to the previous version is one button.

A public repository needs no login. For a private one (a fork, or the old
repository) GitHub wants one, and Lodestone uses a token typed
into Settings if there is one, then `GITHUB_TOKEN`, then the GitHub CLI's login,
then the login git itself already has on this PC (Git Credential Manager) - the
last two are only ever sent to GitHub, and never stored. A fine-grained token
with **Contents: read-only** on the repository is the tidy option.

A folder that is a git checkout of the repository updates with `git fetch` and a
fast-forward instead, and refuses while there are local changes. A packaged
`Lodestone-Engine.exe` cannot replace itself and says so.

### Internet hosting
The **Network** tab decides who can reach your server, in three modes:

| Mode | Who can connect | What it changes |
|---|---|---|
| **Local only** | this PC | `server-ip=127.0.0.1` |
| **LAN** | anything on your network | `server-ip=` (all interfaces) |
| **Internet** | anyone, anywhere | the same, plus a port forward on your router |
| **Tunnel** | anyone, anywhere | the same, plus a playit.gg *or* ngrok agent — no router changes at all |

Internet mode does the whole job: it finds your LAN address, sets
`server-ip=`/`server-port` to match the mode, asks the router to forward the
Minecraft port, checks Windows Firewall, works out your public address, and then
**checks from outside** whether the port actually answers.

**It does not say "online" until something on the internet has connected.** That
is the point. A successful UPnP call is not evidence, and a socket test against
your own machine is not evidence — both can succeed while your friends still get
"connection timed out". The dashboard shows one of: reachable, not reachable,
server offline, port blocked, CGNAT suspected, IPv6 only, or unknown. Unknown is
a real answer and it is used whenever the check could not be completed.

**Automatic port forwarding** tries three protocols in order and tells you what
each one said:

* **UPnP IGD** (v1 and v2) — SSDP discovery, then SOAP against the WAN
  connection service. What almost every home router speaks.
* **NAT-PMP** (RFC 6886) — sent straight at the default gateway.
* **PCP** (RFC 6887) — NAT-PMP's successor, and the one some carrier-grade NAT
  boxes will actually answer.

Only the Minecraft port is ever forwarded, only TCP, and only while internet mode
is on. The lease is renewed at half-life while Lodestone is open, and removed
when you switch the mode off, delete the server, or close the panel. Mappings are
written down in `~/.lodestone.json`, so a crash cannot orphan an open port — the
next launch finds the record and takes it down.

Nothing anyone else made is ever touched. Before deleting, the router is asked
what is currently on that port, and if it no longer points at this machine the
mapping is left alone and the reason is shown.

### Tunnel mode

Port forwarding assumes the router you can configure is the one the internet
arrives at. Behind CGNAT, or behind an ISP box you have no password for, that is
simply false, and no amount of UPnP will rescue it. **Tunnel** mode sidesteps it:
an agent on this machine dials *out* to a relay, and the relay accepts players on
its own public address. Nothing has to be opened on any router.

Two providers, chosen in Network settings:

| | Account step | Address |
|---|---|---|
| **playit.gg** | approve the tunnel once in a browser | `<name>.playit.gg:port` |
| **ngrok** | `ngrok config add-authtoken <token>` once | `N.tcp.ngrok.io:port` |

Switch the mode to Tunnel and Lodestone finds the agent, starts it, reads the
tunnel address out of it, and then — exactly as in Internet mode — asks an
outside service to connect before it will call anything online. A tunnel that is
merely *running* is no more evidence than a port mapping that merely exists.

Both generations of the playit agent are handled, because they are driven
completely differently and guessing wrong hangs: **0.15.x** is a foreground
program whose stdout is read, **1.x** is a Windows service behind a
`playit start|stop|status` CLI. Lodestone detects which one it has.

ngrok is run as `ngrok tcp <port>` — Minecraft is a raw TCP protocol, so it needs
a TCP endpoint, not an HTTP one. The address is read from the agent's logfmt
output, and cross-checked against its local `/api/tunnels` API for the case where
the log line never arrives. On the free plan that address is reissued on every
restart, which the panel says out loud so nobody hands friends a stale one.

What it will not do: download or install either agent, create a playit account,
approve a tunnel, or ask you for an ngrok authtoken. playit prints a claim URL on
first run and Lodestone shows it as a link; ngrok needs
`ngrok config add-authtoken <your token>` run once in a terminal, and Lodestone
only ever checks *whether* a token exists — it never reads, stores, logs or
transmits the value. Both are yours to do, and the panel says so plainly rather
than appearing to hang.

The trade is worth stating: traffic goes through somebody else's servers, latency
rises, and the address belongs to them rather than to you. That is why it is a
mode you choose, not a silent fallback when forwarding fails.

**Auto fix** does everything that can be done from this machine, in order: it
corrects `server-ip`/`server-port` if they do not match the mode, starts the
Minecraft server if it is not running, asks the router for the forward if there
is not one, adds the Windows Firewall rule if nothing allows the port, then
tests again from outside.

It is equally explicit about what it will not do. A router upstream of yours,
CGNAT, an ISP that blocks inbound connections, and an unaccepted EULA are all
outside its reach — so they come back as a list of things for you to do, with
the actual remedy, rather than as a button that pretends and then fails. It
will not accept a licence agreement on your behalf.

**CGNAT** is detected rather than guessed at. If your ISP has put you behind
their own NAT, no amount of port forwarding on your own router will help, and
the panel says so instead of showing a green light. Two independent tells: a
public address inside `100.64.0.0/10`, and a router whose own WAN address
disagrees with what the internet sees. When that happens you get the real
options — IPv6, asking the ISP, forwarding on the outermost router, or a tunnel.

**Windows Firewall** is checked, and one rule can be created for you: inbound,
allow, one protocol, one local port, scoped to the network profile in use. It
asks for administrator rights through the normal UAC prompt, and a refusal is
reported rather than treated as an error. The firewall is never disabled and no
existing rule is ever modified. Removing the rule matches on Lodestone's own
rule name and nothing else.

**Security.** RCON, SFTP, SSH and the panel's own port are refused by the port
picker outright — a Minecraft server on the internet is a game server, an RCON
port on the internet is a remote shell. The tab also audits what publishing
would expose: whitelist off, `online-mode=false`, RCON with no password, RCON
bound to every interface, the panel on `0.0.0.0`, and — on a deep check — any
rule already on your router that forwards a management port to this PC.

Two things reach the outside world and both can be switched off in the tab:
the public-address lookup (`api.ipify.org`, `icanhazip.com`), and the
reachability test (`mcstatus.io`, `mcsrvstat.us`), which necessarily discloses
the address and port being tested because that is what makes it a real test.
With the reachability test off, the status can never rise above "unknown".

### When a remote server will not connect
Press **Test connection** on the server. It walks the stages one at a time —
settings, paramiko, DNS, TCP, SSH handshake, accepted auth methods, sign-in,
login folder, server folder — and marks the one that fails, with the reason. It
also lists what is actually on the account.

The commonest cause is the **server folder** box: on Minehut the files live in
`/server`, not `/`. Lodestone detects that on its own — if the configured folder
has no `server.properties`, it looks for one that does and switches to it, saying
so — and the test offers a one-click fix.

### Reading it
Text size is adjustable and sticks: the **−/+** control in the console and file
editor headers scales both together, `Ctrl` `+`/`−` works while editing, and
Settings has sliders for the mono and interface sizes. The editor also has a
**Wrap** toggle and an **Expand** button that gives it the whole pane — which
matters for Skript, where indentation carries meaning and a narrow column is
unreadable. Tab indents instead of jumping out of the field, and `Ctrl+S` saves.

### Console
Live output, colour-coded by severity, with a filter box, command history on
↑/↓, and one-click macros. The command line talks over stdin when the panel owns
the process and falls back to RCON when it does not — so a server you started by
hand, or one running on a host across the internet, is still controllable.

### RCON
New in the merge, and the thing that makes remote servers genuinely useful.
Turn it on per server (Servers → RCON), point it at the host and port from
`server.properties`, and the console, player moderation, macros and scheduled
tasks all start working against a server this panel never launched.

Needs this in the server's `server.properties`:

```properties
enable-rcon=true
rcon.port=25575
rcon.password=something-long
```

### Minehut
SFTP can move files but cannot start a server, stop it, or run a command. Minehut
has an HTTP API that can, so a Minehut server in Lodestone gets the same controls
as a local one: **start, stop, restart, hibernate, wake**, **send console
commands without RCON**, and a live status badge in the header showing whether it
is online, starting or asleep, with the player count, plan and version.

Turn it on per server under **Servers → Minehut**. It needs the server id (the
hex string in your dashboard URL — the same value as your SFTP username) and the
two credentials the dashboard itself uses: open `dashboard.minehut.com`, press
F12, and copy `minehut_auth_token` and `minehut_session_id` from
**Application → Local Storage**.

Those credentials are stored in `~/.lodestone.json` and are never sent back to
the browser — the settings form shows "saved" and only transmits a replacement
when you type one. They expire, so if actions start failing with a 401, fetch
them again.

⚠️ Minehut publishes no official API. This targets the private one their
dashboard uses, documented by the community, so **it can break whenever Minehut
changes it**. Every call fails with a readable message rather than an exception,
and nothing else in the panel depends on it.

### Worlds
Every folder holding a `level.dat` is listed, with the live one — whatever
`level-name` points at — marked. Switch which is live, download one as a zip,
**reset** it (clears region, entities, poi and level.dat so the server
regenerates it) or delete it outright. Nether and end worlds are labelled. Works
over SFTP, so it covers a hosted server too.

### How the server looks
An MOTD editor with a **live preview** that renders the colour codes the way the
Minecraft server list does — `&a` and `§a` codes, `&#ff8800` and `<#ff8800>` hex,
bold/italic/underline/strikethrough, even the obfuscated flicker. Beside it, the
`server-icon.png` uploader, which checks the file really is a 64×64 PNG under
64 KB before writing it, because Minecraft silently ignores anything else. Plus
max players and the resource-pack settings. On a Minehut server the same changes
are mirrored through their API so the dashboard agrees.

### Files
Browse, edit with conflict detection, rename, copy, delete, upload by drag and
drop, download a file or a whole folder as a zip. If a file changes underneath an
open editor the save is refused and you are offered the overwrite explicitly,
rather than silently clobbering someone else's edit.

### Properties
`server.properties` as a real form — toggles for booleans, dropdowns for
enumerated keys, text for the rest. Comments and key order in the file survive a
save; unknown keys are appended rather than dropped.

### Backups
Zipped and kept on this machine under `~/.lodestone/backups/<server>/`, so remote
worlds get pulled down over SFTP into local archives. Worlds-only or the whole
server folder, on demand or on a timer, with a keep-last-N prune.

Every path in the archive is stored relative to the server root, so a restore
puts each file back exactly where it came from — including multi-world setups
where `world`, `world_nether` and `world_the_end` each keep their own folder.

### Automation
Runs while the panel is open: crash auto-restart, daily restart, a watchdog that
restarts a server whose console has gone silent, periodic `save-all`, repeating
broadcasts, clear-lag, welcome messages, timed commands and named macros.

### Admin assistant
The admin assistant from LocalHost, now pointed through the same backend as
everything else — so it works on remote servers too, running commands over RCON
and editing files over SFTP.

Ask it in plain English ("why did it crash last night", "set the difficulty to
hard and tell everyone", "write me a join-message Skript"). It works step by
step through a fixed action set — read the console, list and read files, write
files, read and set `server.properties`, run commands, moderate players, post to
Discord, take a backup — and finishes with a summary of what it did. Every step
is shown in the transcript, so nothing happens invisibly.

Two switches gate it: **may run commands** and **may edit files**. With both off
it can still read and explain, but cannot change anything. It knows Skript
specifically — file layout, indentation rules, and reloading a script after an
edit.

**Cloud or local.** OpenAI (and anything speaking its shape), Anthropic, Gemini
— or run it entirely on your own machine with **Ollama**, **LM Studio**,
**llama.cpp server**, or any other OpenAI-compatible local server. Local
providers need no API key at all, and nothing leaves your machine.

Press **Detect** and the panel asks the server which models it actually has and
fills the picker — also the quickest way to tell whether Ollama is even running.
Because the assistant demands a bare JSON object every turn, Lodestone switches
on Ollama's `format: "json"` and OpenAI's `response_format` automatically, which
is what makes smaller local models usable here. Temperature is adjustable and
defaults low.

Cloud keys live in `~/.lodestone.json` on your machine and are never sent
anywhere but the provider you pick.

**Automod** watches chat against rules you write and flags violations. Leave
"act on it automatically" off and flags are logged and pushed to Discord for a
human to judge; turn it on and it issues the warn/mute/kick itself.

### AI Assistant
A development assistant that runs on this computer with
[Ollama](https://ollama.com) - free, no account, nothing leaves the machine. It
works inside a **workspace**: every local server is one, and any folder (a
Paper plugin, a Fabric or NeoForge mod) can be added as another. Unlike the
admin assistant it reads the whole project rather than one file, and it is
built for writing code.

**How it works.** Each message is a task. The model is given the project's
shape - build system, platform, Minecraft and Java versions, key files, the
installed Skript and addons, the linked server's state, and notes saved from
earlier conversations - and a set of tools: list, read, find and search files;
check a file; explain an error; write or edit a file; run the build; run an
allowed command; read the console, run a server command, reload a Skript script;
remember a fact; ask you a question; show a plan. It calls tools, reads the
results and carries on until it can answer. The chat shows everything as it
happens: streamed text, each tool call and its result, the plan, and the files
changed.

**Nothing changes without you.** Every file write or edit is an approval card
with the diff and the result of checking the new content (YAML, JSON, TOML,
properties, Skript structure, Java heuristics); every command shows its exact
argument list. Apply, reject with a note (the model reads it and adapts), or
allow the rest of the task. Commands must also be on an allowlist - Gradle,
Maven and their wrappers, `java`/`javac -version` and read-only git by default -
and run without a shell, in the workspace folder. Changed files are backed up
first and **Files changed** puts any of them back. Paths outside the workspace
(unless you give it the whole computer, below), symlinks out of it, `.git` and
binary files are refused before you are asked.
**Stop** ends a task at once - while the model is still reading, while it waits
for you, or while a build runs (only the process tree the task started is
stopped).

**Servers, from any workspace.** The assistant can list every server, start,
stop (cleanly, or by ending the process when a stop does not work) and restart
one, read its console and type commands into it - through the same code as the
Console page's buttons. A start follows the console until the server prints
`Done (…)!` or exits, and hands back the lines that say why. Each action is an
approval card naming the server and how many players are on it.

**Wider reach, when you want it.** *Model & resources → Access and approvals*:

* **Files it can reach** - the workspace (default) or the whole computer. With
  the whole computer it reads anything and writes anywhere except the operating
  system's folders (`C:\Windows`, `Program Files`, `ProgramData`; `/usr`, `/etc`,
  `/boot` and friends), and a **This computer** workspace starts at your home
  folder.
* **Shell** - `run_shell` runs PowerShell on Windows (bash or sh elsewhere) in
  the workspace or a folder it names, with output streamed, a timeout, and Stop
  ending exactly that process tree. *Always allow* remembers that exact command.
* **Approvals** - *ask* (default), *edits* (file changes go ahead, the rest
  asks), or *all* (nothing asks; every file change is still backed up and can
  be put back).
* **Web** - `fetch_url` reads a page as text (asks first unless approvals are
  off); `web_search` uses Ollama's web search API (free account and key, kept
  only in `config.json` and never sent back to the page) or a SearXNG instance.
  Results are marked as someone else's text, and the model is told a page can
  never change what you asked for.

**Which models.** Models that support tool calling use Ollama's native tool
calling; Ollama reports this per model (`/api/show`), and on older Ollama
releases the chat template is checked instead. Other models get the same tools
through a JSON reply format, so they still work, more slowly. The choice can be
forced per model. **Model & resources** lists what is installed with what each
can do. Models marked *tools* at <https://ollama.com/search?c=tool> are the
ones to pull; a coding model of 7-8B parameters (for example
`qwen2.5-coder:7b`) is a reasonable start on a machine with 8 GB of VRAM.

**Resources.** The context length (`num_ctx`, 8192 by default) is sent with
every request, because Ollama reloads a model whose context changes - so a
session never loads the model twice. Model & resources shows the context the
model was trained for, a recommendation for this machine, and how much memory
the context's KV cache needs at each size, worked out from the model's own
shape: bigger is not faster once the model no longer fits the GPU. Keep-alive,
CPU threads, GPU layers, how many assistant tasks may run at once (one by
default), and the step limit are there too.

**Memory and the index.** Each workspace keeps an index of its files, languages
and symbols (classes, methods, Skript commands and functions), brought up to
date by modification time in the background; and notes, which the assistant
saves with its *remember* tool or you add by hand. Both live in
`~/.lodestone/agent/<workspace>/`, with the conversation and the backups.

**The routes only answer this computer.** The assistant runs builds, which run
the project's build scripts, so its routes refuse anything but loopback even
when the panel is opened to the LAN.

### Code
An editor and developer tools for any workspace.

* **Editor** - a file tree and a code editor. Saving refuses to overwrite a file
  that changed on disk since it was opened, backs the old one up (it appears in
  the assistant's Files changed) and checks the new one; problems jump to their
  line.
* **Config files** - YAML, JSON, TOML and `.properties` with line numbers.
  YAML is checked for the mistakes that break plugin configs: tabs, `key: a: b`,
  `&aText` read as an anchor (the colour code silently disappears), `#FF0000`
  read as a comment, unclosed quotes and brackets, duplicate keys.
  `server.properties` is checked against Minecraft's types. Checked for false
  positives against the default configs of EssentialsX, LuckPerms, ViaVersion,
  WorldEdit, PlaceholderAPI and ProtocolLib.
* **Skript** - the installed Skript version and addons (read from their jars),
  each script's commands, events and functions, and its structure checked with
  Skript's own reader rules (`Node.splitLine`, `SectionNode`), which Skript's
  own example scripts pass cleanly. Across the scripts: functions called but
  defined nowhere, commands defined twice, repeating tasks that loop over every
  entity, `while` loops that can hang the server, syntax that appears to need an
  addon that is not installed. **Reload** sends `sk reload <script>` to the
  running server and reads Skript's own verdict back from the console - the
  only authority on whether a line is valid Skript.
* **Java & build** - Gradle or Maven, the platform (Paper, Fabric, Forge,
  NeoForge), Minecraft and Java versions, dependencies, and the mismatches that
  waste an afternoon: the server API packed into your jar, a toolchain too old
  for the Minecraft version, a `plugin.yml` naming a main class that does not
  exist. Build and Test run the project's own wrapper as a job you can follow
  and stop; compile errors from javac, Maven and Kotlin come back with file and
  line. Source heuristics find blocking calls in event handlers and every-tick
  timers.
* **Errors & traces** - paste a stack trace or read the server console: the
  exception chain, the root cause, the frames in this project (linked to their
  files) and the usual causes - Java version mismatches worked out from class
  file versions, API mismatches, async catcher errors, mixin failures.

### Performance
Per server, measured and never estimated:

* **CPU and memory** of the server's process *and its children* - Forge and
  NeoForge start through a script whose child is the real JVM - read from
  `/proc` on Linux and from the kernel on Windows, against the `-Xmx` it was
  given.
* **TPS and MSPT** only as the server reported them, by sending its own command
  and reading the answer: Paper/Purpur `tps` and `mspt`, Spigot `tps`, Forge
  `forge tps`, NeoForge `neoforge tps`, vanilla and Fabric `tick query` (1.20.3+).
  The formats are the ones in each project's source and language files. On
  demand, or every 30 seconds while the page is open; the command and its answer
  appear in the console.
* **Players** from the join and leave lines since the server started.
* **This computer** - memory and CPU; NVIDIA GPUs through `nvidia-smi` when it is
  installed.
* **Ollama** - the models loaded right now, how much of each is on the GPU, and a
  button to unload one.

What cannot be measured says so, and why, instead of showing a number.

**Development Performance Mode** is a switch on this page for coding with a
local model on the same computer. While it is on: one assistant task at a time;
the model stays loaded between requests; background indexing pauses; AI chat
moderation and the automatic update check wait (nothing is lost); this window's
animations stop and the page refreshes less often. It changes only Lodestone's
own work and never touches other programs.

### Discord
Any number of webhooks, each subscribed to its own set of events, so chat can
land in one channel while crashes ping staff in another.

**Sixteen events:** chat, joins and leaves, first-time players, deaths,
advancements, start/stop, crashes, lag warnings, backups, console errors, bans
and kicks read out of the log, in-game player commands, panel commands, panel
moderation, file changes, and scheduled tasks.

**Chat relay** posts as the player, with their skin as the avatar, so the channel
reads like a chat log rather than a bot feed. Busy servers can switch a hook to
**batched** style, which coalesces a burst of messages into one post instead of
one request per line — that is what actually keeps you under the rate limit.

**A live status message.** Turn it on for a hook and Lodestone keeps one message
updated in place — state, player count, uptime, who is online — instead of
posting again every time something changes. It edits the same message forever,
so it behaves like a small dashboard pinned in your channel.

**Threads.** Give a hook a thread ID and its events post there instead of the
channel.

**Crash reports carry the log.** A crash attaches the tail of the console as a
real `console.log` file, so the stack trace is already in the channel.

Per-hook counters show sent/failed and the last error, so a dead webhook is
obvious. Mentions from player chat are stripped, so nobody can `@everyone` your
server by typing it. Rate limits are honoured with backoff and retry.

This is one-way. Discord messages do not come back into the game; that needs a
gateway bot, not a webhook.

### Chat detection
Chat is the one thing that is not standardised. Vanilla logs `<Name> message`,
but Paper logs chat from an **Async Chat Thread**, and any chat plugin — or a
Skript that cancels the chat event and re-broadcasts — rewrites the line
completely, colour codes and rank prefixes and all.

Lodestone reads both shapes: it recognises the chat thread, strips section-sign,
ampersand, hex and MiniMessage markup, splits on the separator, and takes the
last name-shaped token before it. Rank prefixes, prestige numbers and icons fall
away on their own.

If your format is genuinely unusual, the Discord tab has a **Chat detection**
panel: scan the recent log to see what is being picked up and which thread it
arrives on, paste a line to test it, and supply a custom regex with
`(?P<player>…)` and `(?P<message>…)` groups if you need one.

### Tools
**Log search** across `latest.log` and the gzipped rotated archives, with regex
and match-case — the fastest way to answer "why did it die last night". Remote
servers pull each log down over SFTP, so it is capped at twelve files.

**Plugin manager** listing every jar with size and date. Disabling renames to
`.jar.disabled` rather than deleting, which is how server owners already do it
and means nothing is lost. Download or delete from the same row.

**Ops, whitelist and bans** read straight from the server JSON and edited by
sending the real command, so the server stays the source of truth.

### REST API
Off by default. Switch it on in the API tab, mint a token, and drive the panel
from scripts:

```bash
curl http://127.0.0.1:8744/api/v1/status \
  -H "Authorization: Bearer lds_…"
```

```bash
curl -X POST http://127.0.0.1:8744/api/v1/command \
  -H "Authorization: Bearer lds_…" \
  -H "Content-Type: application/json" \
  -d '{"command":"say deploy finished"}'
```

Scopes are `read`, `control`, `files` and `admin`. Tokens are shown once and
stored as a SHA-256 hash — lose one and you mint another. The full endpoint list
is in the API tab.

`POST /api/v1/webhook` fires a custom event into your Discord hooks, which is a
convenient way to get your own build or deploy scripts into the same channel.

### Coming from LocalHost or RemoteHost
On first run Lodestone reads `~/.mc_control_panel.json`,
`~/.mc_sftp_panel.json` and `~/.remotehost_profiles.json` and puts what it finds
on the shelf — local server settings, every saved SFTP profile, the old single
webhook, and your assistant key and rules. If you already have a config, the
Servers tab shows an **Import** banner instead. It skips anything already
present, so importing twice is harmless, and the old config files are never
modified — the old apps still work if you go back to them.

---

## Closing it

**The desktop app** closes the way a desktop app should. If it started the
engine, closing the window stops the engine too. If a Minecraft server is still
running, it asks first: keep it running in the background (open Lodestone again
to get back to it), or stop the server cleanly and quit. An engine that was
already running when the window opened is left running.

**The browser page** is a local web app: the window you see is a browser, and closing it
only closes the browser. The panel itself keeps running in the background,
holding its port — which used to mean the next launch found the port taken and,
in the windowed build, failed with no console to print the reason to. It looked
exactly like clicking the icon did nothing.

Two things fix that:

* **Quit Lodestone**, in Settings. Stops the panel properly — shuts down a
  running Minecraft server first, removes any port forward it created, and
  releases the port.
* **Launching it twice is now safe.** A second launch notices the first, opens
  that window, and exits. A port held by something that is *not* Lodestone is
  still refused, with a real dialog rather than a silent failure.

## Security

**The UI has no login.** Whoever can open the address can control your servers.
It binds to `127.0.0.1` by default. Settings can switch it to `0.0.0.0` for LAN
access — there is still no login, so only do that on a network you trust.

The REST API is the exception: it is off until you enable it and always requires
a bearer token. Enabling it does not give the UI a login.

SFTP and RCON passwords sit in plain text in `~/.lodestone.json`, `chmod 600`
where the OS supports it (a no-op on Windows). This is a self-hosted tool, not a
hardened public service — do not expose it to the open internet without a reverse
proxy and real auth in front.

**Internet hosting publishes one port and one port only.** The Minecraft port,
TCP, and only while internet mode is on. RCON, SFTP, SSH and the panel's own
port are rejected by the port picker — including when RCON is switched on in
`server.properties` rather than in the panel. Turning internet hosting off, or
closing Lodestone, removes the mapping.

What that one port means is worth being plain about: **a Minecraft server on a
public address will be found.** Address ranges are scanned continuously, and
join attempts from strangers usually start within hours. Turn on the whitelist
if you only want your friends in, and leave `online-mode=true` so the server
verifies who people say they are. The Network tab warns about both.

---

## Files

| Path | What |
|---|---|
| `lodestone.py` | The engine, one file — and the classic browser page it still serves |
| `desktop/` | The desktop app: C# + Avalonia, Modrinth-styled. See below |
| `Lodestone.vbs`, `Run Lodestone.bat` | Launchers: the desktop app when built, else the browser page |
| `networking/` | Internet hosting: port mapping, tunnels, public IP, CGNAT, reachability, firewall |
| `creator.py` | Creating servers: which software runs which version, downloading and installing it, finding or fetching Java |
| `content.py` | The mod and plugin browser: Modrinth, Hangar and SpigotMC, compatibility, dependencies, updates |
| `art.py` | Pixel art for each kind of server, 16x16 grids rendered to PNG on request, no image library - the fallback until the textures are here |
| `textures.py` | Instance icons: reads the block and item textures out of Mojang's client jar with range requests, decodes the PNGs, draws items and isometric blocks |
| `Setup Lodestone.bat` | One-click setup on Windows: Python and .NET (via winget), build, shortcuts, start |
| `updater.py` | Self-update from GitHub: check, download, verify, swap in with a backup, go back, and the restart (`--relaunch`) |
| `agent.py` | The AI Assistant: workspaces, the project index, memory, the tool loop (native tool calls or a JSON fallback), approvals, backups, commands, cancelling |
| `websearch.py` | The assistant's web reach: reading a page as text (standard library HTML parser), and search through Ollama's web search API or SearXNG |
| `ollama.py` | Talking to Ollama: models and their capabilities, streaming chat with tools, loaded models, unloading, sizing the context |
| `devtools.py` | Config validation (YAML, JSON, TOML, properties), Gradle/Maven projects, build output, stack traces, Java heuristics |
| `skripttools.py` | Skript: its reader rules, what a script contains, cross-script checks, the installed Skript and addons, and `sk reload` output |
| `perfstats.py` | Process and host CPU and memory (Linux, Windows), NVIDIA GPUs, and TPS/MSPT from each server's own output |
| `make_icon.py` | Draws `Lodestone.ico` / `.png` from a 32×32 pixel grid, no Pillow |
| `build_app.py` | PyInstaller build into a single `Lodestone.exe` |
| `tests/run_all.py` | Every self-test — HTTP, files, backups, RCON, chat parsing, Discord routing, log search, plugins, worlds, icons, the Minehut API, the assistant loop, local models, migration, the whole networking layer, server creation and the mod browser against fakes of each service, and a real parse of the page JavaScript |
| `dist/Lodestone.exe` | The standalone build, ~18 MB, no Python required |

### The networking package

The one part of the app that is not in `lodestone.py`. It is a self-contained
layer with no knowledge of Minecraft servers, and the panel talks to exactly one
class in it — `HostingManager`.

| Module | Class | Job |
|---|---|---|
| `interfaces.py` | `PortMappingProvider`, `PortMapping` | the abstractions everything is written against |
| `errors.py` | `NetworkingError` and friends | one hierarchy, every message printable |
| `net_interfaces.py` | `NetworkInterfaceService` | local IPv4/IPv6, default gateway, private and CGNAT ranges |
| `public_ip.py` | `PublicIpService` | the address the world sees, cached, with change detection |
| `upnp.py` | `UpnpProvider` | SSDP, device description, SOAP, IGD v1 and v2 |
| `natpmp.py` | `NatPmpProvider` | RFC 6886 |
| `pcp.py` | `PcpProvider` | RFC 6887 |
| `port_mapping.py` | `PortMappingService` | provider fallback, the owned-mapping register, lease renewal |
| `tunnel.py` | `TunnelService`, `PlayitProvider`, `NgrokProvider` | playit.gg and ngrok, for when forwarding cannot work |
| `connectivity.py` | `ConnectivityTester` | Server List Ping, the external probe, the state machine |
| `firewall.py` | `FirewallManager` | Windows Firewall, narrowly |
| `hosting.py` | `HostingManager` | the three modes, tied together |

Standard library only — no new dependencies, nothing to install. Adding a fourth
way to open a port (a tunnel, a relay, a VPS) means writing one
`PortMappingProvider` and adding it to the list in
`PortMappingService.providers()`. Nothing above that line changes.

If the package is missing entirely the app still starts: Local and LAN hosting
need none of it, and the Network tab says what is unavailable.

## Notes on the remote transport

paramiko's `SFTPClient` multiplexes a single channel and is **not thread-safe**.
Lodestone tails `logs/latest.log` on a background thread while the UI issues its
own calls, so every SFTP operation is serialised behind one lock. The log tail
takes that lock only if it is free — tailing is best-effort and must never make
you wait behind a large transfer.

This was a real bug, not a precaution: before it was serialised, connecting a
remote server and then touching anything wedged the channel permanently, which
presented simply as "it errors and does not work". `tests/test_sftp.py` runs a
tailer against several concurrent readers and fails if the channel deadlocks.
| `~/.lodestone.json` | Servers, webhooks, automation, API tokens |
| `~/.lodestone/backups/` | Backup archives, one folder per server |

## Tests

```bash
py tests/run_all.py
```

Covers the HTTP surface and API scoping, file operations and path containment,
`server.properties` round-trips, backup and restore (including multi-world
layouts), the RCON protocol against a mock server, Discord event routing and
filtering, chat parsing across eleven real server formats plus eight lines that
must *not* be read as chat, log search through gzipped archives, plugin
enable/disable, the ops and whitelist lists, the assistant action loop with a
scripted model, the Ollama and OpenAI-compatible request shapes, the Minehut API
against a fake host that speaks the real protocol, the world manager, PNG icon
validation, migration from the old configs, and the whole SFTP layer against a
real SFTP server — including the staged diagnosis, the root auto-correction, and
a concurrency test that would deadlock without the channel lock.

`test_creator.py` covers server creation and the mod and plugin browser the
same way: Mojang's manifest, PaperMC's Fill builds, Modrinth's search, versions
and hash lookups, Hangar's versions and Spiget's resources are each a real HTTP
server speaking that service's shape. It checks the version filtering, that a
checksum mismatch fails and leaves nothing behind, dependency resolution
(including Hangar naming a dependency by number and installing it by slug),
updates that keep config folders, and a whole create job — plus one that fails
half way and must leave no folder and no shelf entry.

`test_textures.py` fetches the textures from a fake Mojang that serves the
manifest, a version file and a client jar, and answers Range requests the way
piston-data does: only the jar's index and the texture stretch are read, never
the whole file, and a server that ignores Range still works. Its PNGs are
written in the formats Mojang ships (4- and 8-bit palettes with a transparent
index, greyscale with a transparent grey) and every row filter is checked.
Then the drawing - a scaled item keeps hard edges, a block's top is tinted grass
green and its right side shaded darker - and the engine's side: the icon a
server gets automatically, a picked one kept, and names that are not icons refused.

`test_updater.py` runs the self-update against a fake GitHub (the commits list
filtered by folder, and zipballs named after their commit): a commit outside the
folder is not an update, a zip of the wrong commit or with a broken Python file
is refused, user files and build output survive, a file in use rolls the whole
update back, and the restart is run for real - including a new version that
never answers and has to be swapped back for the old one.

`test_networking.py` covers internet hosting against fakes that speak the real
protocols on the wire, because the bugs this layer has are byte-layout bugs and
a mock returning a Python dict would catch none of them: a **fake UPnP IGD**
over real HTTP with real device-description XML, real SOAP and real SOAP faults
(conflict 718, unauthorised 606, permanent-leases-only 725); a **fake NAT-PMP
responder** and a **fake PCP server** speaking real RFC 6886 and RFC 6887
datagrams, nonce checking included; a **fake Minecraft server** speaking a real
Server List Ping handshake; a captured `netsh` rule table; and fake public-IP
and reachability endpoints.

It also asserts the properties that matter more than any single function:

* nothing reports "reachable" without an outside connection that can be
  attributed to *this* server — a Minecraft server answering on a shared CGNAT
  address is explicitly not enough;
* a reachability verdict expires, and is dropped outright when the port changes
  or the mapping goes away, so a green light can never outlive its evidence;
* a management port cannot be published through any door, including the REST
  API and a `server.properties` that enables RCON behind the panel's back;
* release refuses to delete a mapping that no longer points at this machine;
* a corrupt mapping register is ignored rather than fatal;
* an SSH daemon on port 25565 is not mistaken for a Minecraft server, though a
  plain TCP check would pass it.

Several of those tests failed the first time they were run, which is why they
are phrased as properties rather than as unit tests of the happy path.

Because the entire UI is one JavaScript string inside a Python file, a stray
quote produces a valid Python file that serves a dead page — so one suite feeds
the generated page to `node --check`, and cross-checks that every inline
`onclick` names a function that exists and every rail entry has a matching view.

The suite runs against a throwaway `HOME`, so it never reads or writes your real
`~/.lodestone.json`, your servers, or your backups.

`test_ollama.py`, `test_agent.py` and `test_dev_http.py` run the assistant
against a fake Ollama that speaks Ollama's documented wire format - streamed
NDJSON, tool calls, `/api/show`, `/api/ps` - so the whole loop is exercised
without a model: reading, editing with approval, rejecting with a note,
reverting, refused paths and commands, builds whose errors are parsed, trusted
commands, cancelling while the model streams, while it waits for approval and
while a build runs (checking the build's process is really gone), the JSON
fallback, a tool call written as text, questions, memory, plans, the step limit,
Skript reloads through a fake server, one task at a time, and trimming to the
context. The HTTP suite also runs a fake server process that answers `tps`,
`mspt` and `sk reload` exactly as Paper and Skript do.

`test_devtools.py` covers the checks with the inputs server owners have, and
`test_perfstats.py` measures real processes on the machine running it.

## Building an exe

```bash
py build_app.py
```

Produces `dist/Lodestone.exe` with the icon embedded. `paramiko` is bundled if
it is installed in the environment you build from; without it the exe still runs
and handles local servers, and remote servers report what is missing.

`py build_app.py --name Lodestone-Engine` builds the same thing under another
name; `desktop/build.py --engine-exe` uses that to bundle a frozen engine.

---

## The desktop app

`desktop/` is Lodestone as a native window — C# 12, .NET 8 and Avalonia 11, so
it runs on Windows, Linux and macOS. It looks like the Modrinth app: Modrinth's
own colour tokens (taken from modrinth.com's CSS), Inter, rounded 16 px cards,
green brand buttons with black text, pill tabs, grey tag chips, a frame with the
navigation on the left and the page on a darker sheet with a rounded corner. The
server tabs open under a header laid out like a Modrinth project page: icon,
name, status, tags, and Start in green or Stop in red.

It has every tab the browser page has — console, files, properties, backups,
tools, automation, assistant, Discord, servers, network, API, settings — and a
few things a page could not do well:

* **The file editor is a real code editor** (AvaloniaEdit): line numbers, Tab and
  Shift+Tab indent blocks, Ctrl+S saves, Ctrl +/- resizes.
* **The console** is a coloured, selectable log that copes with thousands of
  lines, with stat tiles above it the way Modrinth's server panel has them.
* **Native file dialogs** for uploads, downloads, the server icon, the JAR and
  the server folder. Files dropped on the Files tab upload into the folder on screen.
* **Switching servers restarts the console stream**, so the new server's lines
  always show. The browser page reused the old stream's position and could miss them.

### How it runs

The app is only the window. On start it looks for a Lodestone already answering
on the configured port and adopts it (it may be running a server). Otherwise it
starts one, in this order:

1. `--engine <path>` or the `LODESTONE_ENGINE` variable — a `.py` or a frozen engine
2. `engine/Lodestone-Engine(.exe)` beside the app — the `--engine-exe` build
3. `engine/lodestone.py` beside the app, or `lodestone.py` in a folder above it,
   run with `pyw`/`pythonw` (no console window), or `python3` elsewhere

The engine runs with `--headless` (no browser) and `--log`, so if it fails to
start, the app shows its log instead of a blank window. The app's own settings
(text sizes, window size) and that log live in `%APPDATA%\Lodestone`
(`~/.config/Lodestone` elsewhere).

### Building it

Needs the .NET 8 SDK (or newer). On Windows, `Setup Lodestone.bat` does all of
this for you; by hand:

```bash
py desktop/build.py                      # Windows: dist/win-x64/Lodestone.exe, self-contained
py desktop/build.py --rid linux-x64      # also osx-arm64, osx-x64, win-arm64
py desktop/build.py --dll                # dist/dll: Lodestone.dll + Lodestone.vbs
py desktop/build.py --engine-exe         # also freeze the engine, so Python is not needed
```

Every build copies the engine into `engine/` beside the app. **On this PC use
`--dll`**: the antivirus refuses freshly built `.exe` files, but `Lodestone.dll`
runs through Microsoft's signed `dotnet.exe` and is not blocked. `Lodestone.vbs`
in this folder prefers that build, and points it at `lodestone.py` here, so
engine edits take effect without rebuilding the app.

`desktop/nuget.config` exists because the machine-wide NuGet config on this PC
has no package sources; it scopes nuget.org to this project.

### Checking the look without a screen

```bash
dotnet desktop/bin/Debug/net8.0/Lodestone.dll --shots out --port 18744
```

`--shots` renders every tab (and a few dialogs) to PNGs headlessly, against an
engine already running on that port. Start one against a throwaway home, as the
tests do, and it never touches your real config.
