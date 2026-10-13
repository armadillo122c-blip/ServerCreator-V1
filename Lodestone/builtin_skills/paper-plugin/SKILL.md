---
name: paper-plugin
description: Create or extend a Paper (Bukkit/Spigot-compatible) server plugin in Java with Gradle - project layout, plugin.yml, commands, events, building the jar and trying it on a server. Use when the user wants a new plugin or a change to one.
---

# Paper plugins

## Do it, don't describe it

You have the tools for every step - create the project, write the code, build it, put it on the server, restart it. Do not answer with instructions for the user.

1. Find out the server's Minecraft version (`list_servers` or `server_status`) and whether a plugin project exists already (`project_overview`).
2. No project yet: `create_project` with template `paper-plugin`, a name and the server's Minecraft version. It writes the build file, plugin.yml and a main class compiled for the right Java, and opens the project to you - write its files by their full path.
3. Write the code (see below) with `write_file` / `edit_file`: the main class, the command in `plugin.yml`.
4. `run_build` with `folder` = the project folder. If Gradle or the JDK is missing, `install_tool` (gradle, and jdk with the Java version the build file uses), then build again. Fix every compile error it reports.
5. `install_jar` with the jar from `<project>/build/libs/`, then `restart_server`, then `read_console` (filter: the plugin's name) to see it enable.

## What create_project writes (Gradle, Kotlin DSL)

```
my-plugin/
  settings.gradle.kts        rootProject.name = "my-plugin"
  build.gradle.kts
  src/main/java/<package>/MyPlugin.java
  src/main/resources/plugin.yml
```

`build.gradle.kts`:

```kotlin
plugins {
    java
}

group = "com.example"
version = "1.0.0"

repositories {
    mavenCentral()
    maven("https://repo.papermc.io/repository/maven-public/")
}

dependencies {
    compileOnly("io.papermc.paper:paper-api:<minecraft version>-R0.1-SNAPSHOT")
}

tasks.withType<JavaCompile>().configureEach {
    options.encoding = "UTF-8"
    options.release.set(21)   // the Java the server's Minecraft version needs
}

tasks.processResources {
    filesMatching("plugin.yml") { expand("version" to project.version) }
}
```

Use the server's exact Minecraft version in the dependency (for example `1.21.4-R0.1-SNAPSHOT`). If the build cannot find that version, look at what exists at https://repo.papermc.io/repository/maven-public/io/papermc/paper/paper-api/maven-metadata.xml (fetch_url, if the web is on) rather than guessing. The JDK building it must be at least the `release` version - `install_tool` jdk with that number if the build says the release is not supported.

`plugin.yml`:

```yaml
name: MyPlugin
version: '${version}'
main: com.example.myplugin.MyPlugin
api-version: '1.21'
commands:
  hello:
    description: Says hello
    usage: /hello
    permission: myplugin.hello
permissions:
  myplugin.hello:
    default: true
```

`api-version` is the major.minor version ('1.21'), not the full one. Prefer `plugin.yml`; only use `paper-plugin.yml` if the project already does - it registers commands differently (Brigadier through the lifecycle API) and has no `commands:` section.

## Code

- The main class `extends JavaPlugin`; set things up in `onEnable()`, clean up in `onDisable()`.
- Events: a class that `implements Listener` with `@EventHandler` methods, registered with `getServer().getPluginManager().registerEvents(listener, this)`.
- Commands declared in plugin.yml: `getCommand("hello").setExecutor(executor)`, where the executor implements `CommandExecutor` (`onCommand(CommandSender sender, Command command, String label, String[] args)` returning `true` when handled). Add a `TabCompleter` for suggestions.
- Text: Paper ships Adventure - `player.sendMessage(Component.text("Hi", NamedTextColor.GREEN))`, or MiniMessage (`MiniMessage.miniMessage().deserialize("<green>Hi")`). Avoid the deprecated `ChatColor` strings in new code.
- Never block the main thread (no `Thread.sleep`, no network or database calls in event handlers); use `getServer().getScheduler().runTaskAsynchronously(this, ...)` and come back with `runTask` before touching the world or players.
- Configuration: `saveDefaultConfig()` with a `src/main/resources/config.yml`, read with `getConfig()`.

## Build and try it

1. `run_build` with the project folder; the jar is in `build/libs/`. Fix every compile error the build reports and build again.
2. `install_jar` puts it in the server's `plugins/` folder; then `restart_server` - not `/reload`, which leaves plugins half-reloaded.
3. Read the console (`read_console` with filter = the plugin name) for "Enabling MyPlugin" and any errors, and try the command with `server_command` if it can run from the console.

A `/spawn` command, for example: in `onCommand`, if the sender is a `Player`, `player.teleport(player.getWorld().getSpawnLocation())` and tell them; otherwise say only players can use it. Declare `spawn` under `commands:` in plugin.yml.

Say at the end which files you made, how you built it, and what you saw on the server.
