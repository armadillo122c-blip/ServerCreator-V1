---
name: paper-plugin
description: Create or extend a Paper (Bukkit/Spigot-compatible) server plugin in Java with Gradle - project layout, plugin.yml, commands, events, building the jar and trying it on a server. Use when the user wants a new plugin or a change to one.
---

# Paper plugins

## Before writing code

1. `project_overview` - is there already a build (build.gradle(.kts) or pom.xml), a `plugin.yml` or `paper-plugin.yml`, and which Minecraft version does the server run?
2. Match the server: the API version is the server's Minecraft version. Paper for Minecraft 1.20.5 and newer needs Java 21.
3. If the user has no project yet, make one in a new folder they choose (or ask where).

## A new project (Gradle, Kotlin DSL)

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

java {
    toolchain.languageVersion.set(JavaLanguageVersion.of(21))
}

tasks.processResources {
    filesMatching("plugin.yml") { expand("version" to project.version) }
}
```

Use the server's exact Minecraft version in the dependency (for example `1.21.4-R0.1-SNAPSHOT`). If unsure which versions exist, check https://repo.papermc.io/repository/maven-public/io/papermc/paper/paper-api/ (with fetch_url) rather than guessing.

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

1. `run_build` (or `./gradlew build`); the jar is in `build/libs/`. Fix every compile error the build reports and build again.
2. Copy the jar into the server's `plugins/` folder (write it with the shell or ask the user), then `restart_server` - not `/reload`, which leaves plugins half-reloaded.
3. Read the console (`read_console` with filter = the plugin name) for "Enabling MyPlugin" and any errors, and try the command with `server_command` if it can run from the console.

Say at the end which files you made, how you built it, and what you saw on the server.
