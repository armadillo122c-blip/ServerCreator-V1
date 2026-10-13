---
name: fabric-mod
description: Create or change a Fabric mod for Minecraft - project setup from the official template, fabric.mod.json, entrypoints, Fabric API, mixins, building and testing. Use when the user wants a Fabric mod (not a Bukkit/Paper plugin).
---

# Fabric mods

## Versions come from Fabric, not from memory

Fabric's tooling changes with Minecraft versions. Before writing a build file, read the current values:

- https://fabricmc.net/develop/ - for a Minecraft version: the Loader version, Loom version, Fabric API version, and the mappings setup.
- New projects: the official template generator at https://fabricmc.net/develop/template/ produces a ready project. Tell the user to generate one there (or use one they have) instead of writing every Gradle file by hand - Loom's setup is easy to get subtly wrong.

Use `fetch_url` for those pages if the web is turned on; otherwise ask the user for the versions shown there.

## The pieces

- `gradle.properties` holds the versions (`minecraft_version`, `loader_version`, `fabric_version`, mappings, `loom_version` or the plugin version in build.gradle).
- `build.gradle` applies the `fabric-loom` plugin and depends on `minecraft`, the mappings, `fabric-loader` and (usually) `fabric-api`.
- `src/main/resources/fabric.mod.json`:

```json
{
  "schemaVersion": 1,
  "id": "examplemod",
  "version": "${version}",
  "name": "Example Mod",
  "environment": "*",
  "entrypoints": {
    "main": ["com.example.ExampleMod"],
    "client": ["com.example.ExampleModClient"]
  },
  "mixins": ["examplemod.mixins.json"],
  "depends": {
    "fabricloader": ">=0.16.0",
    "minecraft": "~1.21.4",
    "java": ">=21",
    "fabric-api": "*"
  }
}
```

  The `id` is lowercase letters, digits, `-` and `_`. Fill the version numbers from the project, not from this example.

- The main entrypoint implements `ModInitializer` (`onInitialize()`); client-only code goes in a `ClientModInitializer` (`onInitializeClient()`) and must not be loaded on a dedicated server.
- Prefer Fabric API events and registries over mixins. When a mixin is needed, keep it small, target the right side (client/server/common), and list it in the mixins JSON.

## Build and test

1. `run_build`; the mod jar is in `build/libs/` (the one without `-sources`). Fix compile errors and build again.
2. `./gradlew runServer` / `runClient` start a development game; or copy the jar into a Fabric server's `mods/` folder together with Fabric API, then `restart_server` and read the console for the mod loading and for mixin errors ("Mixin apply failed" means the target changed - check the mappings and method names).

Say which files you made, the versions you used and where they came from, and what the test showed.
