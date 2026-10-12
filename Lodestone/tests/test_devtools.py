"""Config validation, Skript reading, Java projects, build output and stack
traces - with the inputs server owners actually have."""
import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import devtools as dt
import skripttools as sk

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


def msgs(res, sev=None):
    return [(p["line"], p["message"]) for p in res["problems"] if sev is None or p["severity"] == sev]


def has(res, line, needle, sev=None):
    return any(p["line"] == line and needle.lower() in p["message"].lower() and (sev is None or p["severity"] == sev)
               for p in res["problems"])


# ---------------------------------------------------------------------------
section("YAML that is fine")
GOOD = """# EssentialsX-style config
settings:
  prefix: '&8[&6Server&8] '
  hex: "#FF8800"
  motd:
    - "&aWelcome, {PLAYER}!"
    - '&7Type /help'
  cooldowns:
    heal: 30
    feed: 10
  url: https://example.com/a:b
  description: A long line that
    keeps going on the next one
  disabled-worlds: [world_nether, world_the_end]
  multi: |
    line one: with a colon
    # not a comment inside a block
  list:
  - one
  - two
  after-list: true
database:
  type: sqlite
"""
r = dt.check_yaml(GOOD)
check("a normal plugin config has no problems", r["ok"] and not r["problems"], msgs(r))
check("top-level keys are collected", set(r["top"]) == {"settings", "database"}, r["top"])

section("YAML mistakes that break plugin configs")
r = dt.check_yaml("settings:\n\tprefix: x\n")
check("a tab is an error", has(r, 2, "tab", "error"), msgs(r))
r = dt.check_yaml("settings:\n  prefix: '&aHi'\n   extra: 1\n")
check("a line indented deeper than a value is an error", has(r, 3, "deeper", "error"), msgs(r))
r = dt.check_yaml("a:\n    b: 1\n  c: 2\n")
check("a dedent to no level is an error", has(r, 3, "does not match", "error"), msgs(r))
r = dt.check_yaml("message: Hello: world\n")
check("': ' inside an unquoted value is an error", has(r, 1, "starts another key", "error"), msgs(r))
r = dt.check_yaml("join-message: &aWelcome back\n")
check("&a colour text read as an anchor is flagged", has(r, 1, "anchor", "warning"), msgs(r))
check("with the fix quoted", "'&aWelcome back'" in r["problems"][0].get("hint", ""), r["problems"])
r = dt.check_yaml("color: #FF0000\n")
check("# read as a comment is flagged", has(r, 1, "comment", "warning"), msgs(r))
r = dt.check_yaml("title: *Important*\n")
check("* read as an alias is an error", has(r, 1, "anchor that does not exist", "error"), msgs(r))
r = dt.check_yaml("base: &defaults\n  a: 1\nother: *defaults\n")
check("a real anchor and alias are fine", r["ok"], msgs(r))
r = dt.check_yaml("prefix: '&6Shop\nnext: 1\n")
check("an unclosed quote is an error", not r["ok"] and any("never closed" in m for _l, m in msgs(r)), msgs(r))
r = dt.check_yaml("worlds: [world, nether\nnext: 1\n")
check("an unclosed bracket is an error", not r["ok"] and any("never closed" in m for _l, m in msgs(r)), msgs(r))
r = dt.check_yaml("a: 1\nb: 2\na: 3\n")
check("a duplicate key is a warning naming the first", has(r, 3, "line 1", "warning"), msgs(r))
r = dt.check_yaml("max-players:20\n")
check("key:value without a space is caught", has(r, 1, "no space after the colon", "error"), msgs(r))
r = dt.check_yaml("msg: 'it''s fine' trailing\n")
check("text after a closing quote is an error", has(r, 1, "after the closing quote", "error"), msgs(r))
r = dt.check_yaml("cmd: @everyone\n")
check("a reserved @ is an error", has(r, 1, "cannot start with '@'", "error"), msgs(r))
r = dt.check_yaml("msg: \"multi\n  line\"\nnext: 1\n")
check("a double-quoted value may span lines", r["ok"], msgs(r))

section("plugin.yml")
r = dt.validate("plugin.yml", "name: My Plugin\nversion: 1.0\n")
check("missing main is an error", any("'main'" in m for _l, m in msgs(r, "error")), msgs(r))
check("a name with a space is an error", any("may only use letters" in m for _l, m in msgs(r)), msgs(r))
check("missing api-version is a warning", any("api-version" in m for _l, m in msgs(r, "warning")), msgs(r))
r = dt.validate("plugin.yml", "name: Ok\nversion: 1\nmain: a.B\napi-version: '1.21'\ndepend: [Vault]\n")
check("a complete plugin.yml is fine", r["ok"] and not r["problems"], msgs(r))

section("JSON, TOML and properties")
r = dt.check_json('{"a": 1,\n "b": 2,}\n')
check("a trailing comma is an error with a line", not r["ok"] and r["problems"][0]["line"] == 2, r["problems"])
r = dt.check_json('{"a": 1, "a": 2}')
check("a duplicate JSON key is a warning", r["ok"] and r["warnings"] == 1, r["problems"])
r = dt.check_json('\ufeff{"pack": {"pack_format": 34}}')
check("a BOM does not break JSON", r["ok"], r["problems"])
r = dt.check_toml('[mods]\nmodId = "x"\nbad = \n')
if dt.tomllib:
    check("a TOML error has a line", not r["ok"] and r["problems"][0]["line"] == 3, r["problems"])
else:
    check("TOML says it needs a newer Python", "3.11" in r["problems"][0]["message"])
check("good TOML is fine", dt.check_toml('[a]\nb = 1\n')["ok"])
BOOL = {"pvp", "online-mode", "white-list"}
ENUM = {"difficulty": ["peaceful", "easy", "normal", "hard"], "gamemode": ["survival", "creative", "adventure", "spectator"]}
props = "#Minecraft server properties\npvp=yes\nserver-port=99999\nmax-players=abc\ndifficulty=2\ngamemode=hardcore\nview-distance=64\nmotd=A \\\n  continued line\npvp=true\nmotd2=\\u00A7aGreen \\uZZZZ\n"
r = dt.check_properties(props, server=True, bool_keys=BOOL, enum_keys=ENUM)
check("a non-boolean is an error", has(r, 2, "true or false", "error"), msgs(r))
check("a port out of range is an error", has(r, 3, "65535", "error"), msgs(r))
check("a non-number is an error", has(r, 4, "whole number", "error"), msgs(r))
check("numeric difficulty is the old form, not an error", has(r, 5, "old numeric", "info"), msgs(r))
check("an unknown gamemode is an error", has(r, 6, "must be one of", "error"), msgs(r))
check("view-distance 64 is a warning", has(r, 7, "accepts", "warning"), msgs(r))
check("a duplicate key is a warning", has(r, 10, "set again", "warning"), msgs(r))
check("a bad \\u escape is an error", has(r, 11, "\\uXXXX", "error"), msgs(r))
parsed = dict((k, v) for _l, k, v in dt.parse_properties(props))
check("a continued line is joined", parsed.get("motd") == "A continued line", parsed.get("motd"))

# ---------------------------------------------------------------------------
section("Skript: the reader's own rules")
check("'#' in a string is not a comment", sk.split_line('send "#1 player" to player # hi', False)[0] == 'send "#1 player" to player ')
check("'##' outside a string is an escaped #", sk.split_line("set {_x} to 1 ## 2", False)[0] == "set {_x} to 1 # 2")
check("'#' inside %expr% inside a string ends the line", sk.split_line('send "%{x}#y%"', False)[1].startswith("#"))
v, c, blk = sk.split_line("###", False)
check("### opens a block comment", blk is True and v == "")
GOODSK = """options:
    prefix: &6[Shop]

command /heal [<player>]:
    permission: shop.heal
    trigger:
        if arg-1 is set:
            heal arg-1
        else:
            heal player
        send "{@prefix} healed!" to player

on join:
    set {joins::%uuid of player%} to 1
    add 1 to {total.joins}
    send "Welcome! You are join number %{total.joins}%" to player

function greet(p: player, msg: text = "hi") :: text:
    return "%{_msg}% %{_p}%"

every 5 minutes:
    broadcast "Remember to vote!"
"""
nodes, probs, unit = sk.read_nodes(GOODSK)
check("a tidy script reads with no problems", probs == [], probs)
check("the indent unit is the first indented line", unit == "    ")
r = sk.check(GOODSK, "shop.sk")
check("check() agrees", r["ok"] and r["errors"] == 0 and r["warnings"] == 0, r["problems"])
info = r["info"]
check("the command is found with its permission", info["commands"] and info["commands"][0]["name"] == "heal"
      and info["commands"][0]["permission"] == "shop.heal", info["commands"])
check("the event is found", info["events"] and info["events"][0]["event"] == "join", info["events"])
check("the function and its return type are found", info["functions"] and info["functions"][0]["name"] == "greet"
      and info["functions"][0]["returns"] == "text", info["functions"])
check("the periodic task is read in ticks", info["periodic"][0]["ticks"] == 6000, info["periodic"])
check("options are read", info["options"].get("prefix") == "&6[Shop]", info["options"])
check("list and global variables are told apart", "joins::*" in info["variables"]["list"]
      and "total.joins" in info["variables"]["global"], info["variables"])
check("local variables are kept apart", "_msg" in info["variables"]["local"], info["variables"])

BAD = "on join:\n    send \"a\"\n        send \"b\"\non quit:\ncommand /x:\n    permission: a.b\non chat:\n  \tsend \"x\"\n"
nodes, probs, unit = sk.read_nodes(BAD)
check("a line deeper than its section is Skript's indentation error",
      any(p["line"] == 3 and p["message"].startswith("indentation error: expected 4 spaces, but found 8 spaces") for p in probs), probs)
check("an empty section is Skript's warning", any(p["line"] == 4 and p["message"].startswith("Empty configuration section") for p in probs), probs)
check("mixed whitespace that is not a multiple is an error", any(p["line"] == 8 and "indentation error" in p["message"] for p in probs), probs)
r = sk.check(BAD, "bad.sk")
check("a command without trigger is flagged", any(p["line"] == 5 and "trigger" in p["message"] for p in r["problems"]), r["problems"])
nodes, probs, unit = sk.read_nodes("on join:\n\t \tsend \"x\"\n")
check("a first indent mixing tabs and spaces is Skript's error",
      any("not mixed" in p["message"] for p in probs), probs)
nodes, probs, unit = sk.read_nodes("###\non join:\n    x\n")
check("an unclosed block comment is an error", any("never closed" in p["message"] for p in probs), probs)

section("Skript: costly patterns")
COST = """every tick:
    loop all entities:
        if loop-entity is a zombie:
            kill loop-entity

every 1 second:
    loop all players:
        send action bar "hi" to loop-player

on right click:
    while {_x} is less than 10:
        add 1 to {_x}
    loop all blocks in radius 40 around player:
        set loop-block to air
"""
r = sk.check(COST, "cost.sk")
check("every tick is a warning", any(p["line"] == 1 and "every tick" in p["message"] for p in r["problems"]), r["problems"])
check("looping all entities on a fast timer is a warning",
      any(p["line"] == 1 and "loops over every entity" in p["message"] for p in r["problems"]), r["problems"])
check("looping all players every second is only info",
      any(p["line"] == 6 and p["severity"] == "info" for p in r["problems"]) or
      not any(p["line"] == 6 and p["severity"] == "warning" for p in r["problems"]), r["problems"])
check("a while loop that can end is only noted", any(p["line"] == 11 and "while loop" in p["message"] and p["severity"] == "info" for p in r["problems"]), r["problems"])
r2 = sk.check("on join:\n    while true:\n        add 1 to {_x}\n", "hang.sk")
check("a while true loop with no exit is a warning", any(p["line"] == 2 and p["severity"] == "warning" for p in r2["problems"]), r2["problems"])
r2 = sk.check("using queues\n\non join:\n    stop\n", "flags.sk")
check("'using <feature>' at the top level is not flagged", not r2["problems"], r2["problems"])
check("a radius-40 block loop says how many blocks", any(p["line"] == 13 and "268" in p["message"] for p in r["problems"]), r["problems"])
r = sk.check("on join:\n    while {_x} < 5:\n        add 1 to {_x}\n        wait 1 tick\n", "ok.sk")
check("a while loop that waits is fine", not any("while loop" in p["message"] for p in r["problems"]), r["problems"])

section("Skript: across scripts")
A = 'function reward(p: player):\n    give 1 diamond to {_p}\n'
B = ('command /claim:\n    trigger:\n        reward(player)\n        rewrd(player)\n        send "%round(1.5)%"\n'
     '        set {_n} to size of (all players)\n\ncommand /heal:\n    trigger:\n        heal player\n')
C = ('command /heal:\n    trigger:\n        heal player\n        open {_gui} to player\n'
     '        create a gui with virtual chest inventory with 3 rows named "x"\n'
     '        send "%{coins::%player%}%"\n')
rep = sk.workspace_report({"a.sk": A, "b.sk": B, "c.sk": C},
                          {"skript": {"version": "2.9.5"}, "addons": [{"name": "SkBee", "version": "3.6"}]})
cross = rep["cross"]
check("a function from another script is found", not any("reward()" in p["message"] for p in cross), cross)
check("a misspelled function is flagged", any("rewrd()" in p["message"] for p in cross), cross)
check("built-in functions are not flagged", not any("round()" in p["message"] for p in cross), cross)
check("'size of (x)' is not read as a call", not any("of()" in p["message"] for p in cross), cross)
check("the same command in two scripts is flagged", any("/heal is also defined" in p["message"] for p in cross), cross)
check("gui syntax with skript-gui missing is flagged", any("skript-gui" in p["message"] and p["file"] == "c.sk" for p in cross), cross)
check("an installed addon is not reported missing", not any(m["addon"] == "SkBee" for m in rep["missing_addons"]))
check("a variable read but never set is noted", any("{coins::*}" not in p["message"] for p in cross))

section("Skript: what sk reload printed")
LOG = [
    "[12:00:01 INFO]: [Skript] Reloading shop.sk...",
    "[12:00:01 ERROR]: [Skript] Line 7: (shop.sk)",
    "    Can't understand this condition/effect: sendd \"hi\" to player",
    "    Line: sendd \"hi\" to player",
    " ",
    "[12:00:01 ERROR]: [Skript] Line 12: (shop.sk)",
    "    'loop-player' can only be used in a loop over players",
    "    Line: send \"x\" to loop-player",
    "[12:00:01 INFO]: [Skript] Encountered 2 errors while reloading shop.sk! (31ms)",
]
p = sk.parse_reload(LOG, "shop")
check("errors are read with their lines", [e["line"] for e in p["entries"]] == [7, 12], p)
check("with Skript's message", "Can't understand this condition/effect" in p["entries"][0]["message"], p["entries"])
check("and the offending code", p["entries"][0]["code"] == 'sendd "hi" to player', p["entries"])
check("the summary gives the count and time", p["status"] == "errors" and p["error_count"] == 2 and p["ms"] == 31, p)
check("with the summary matching, plain entries are errors", all(e["severity"] == "error" for e in p["entries"]), p)
OK = ["[12:00:05 INFO]: [Skript] Reloading shop.sk...",
      "[12:00:05 INFO]: [Skript] Successfully reloaded shop.sk. (12ms)"]
p = sk.parse_reload(OK, "shop")
check("a clean reload is ok", p["status"] == "ok" and not p["entries"] and p["ms"] == 12, p)
ANSI = ["\x1b[0;33;1m[Skript] Line 3: (warn.sk)\x1b[m", "    Variables cannot be used here.", "    Line: x",
        "[Skript] Successfully reloaded warn.sk. (4ms)"]
p = sk.parse_reload(ANSI, "warn")
check("a gold entry with a successful reload is a warning", p["status"] == "warnings" and p["entries"][0]["severity"] == "warning", p)
check("nothing printed is said plainly", sk.parse_reload(["[12:00] [Server thread/INFO]: Done"], "x")["status"] == "no-output")

section("Skript: the installed environment")
srv = tempfile.mkdtemp()
os.makedirs(os.path.join(srv, "plugins", "Skript", "scripts", "sub"))


def jar(name, yml):
    with zipfile.ZipFile(os.path.join(srv, "plugins", name), "w") as z:
        z.writestr("plugin.yml", yml)


jar("Skript-2.9.5.jar", "name: Skript\nversion: 2.9.5\nmain: ch.njol.skript.Skript\n")
jar("SkBee-3.6.jar", "name: SkBee\nversion: 3.6.0\nmain: a.B\ndepend: [Skript]\n")
jar("skript-reflect.jar", "name: skript-reflect\nversion: '2.5'\nmain: a.C\nsoftdepend:\n  - Skript\n")
jar("Vault.jar", "name: Vault\nversion: 1.7\nmain: a.D\n")
open(os.path.join(srv, "plugins", "Skript", "scripts", "shop.sk"), "w").write(GOODSK)
open(os.path.join(srv, "plugins", "Skript", "scripts", "sub", "-old.sk"), "w").write("on join:\n    stop\n")
env = sk.environment(srv)
check("Skript's version is read from its jar", env["skript"] and env["skript"]["version"] == "2.9.5", env)
check("addons are what depend on Skript", sorted(a["name"] for a in env["addons"]) == ["SkBee", "skript-reflect"], env["addons"])
check("scripts are listed, disabled ones marked", [(s["name"], s["disabled"]) for s in env["scripts"]] ==
      [("shop.sk", False), ("sub/-old.sk", True)], env["scripts"])
shutil.rmtree(srv)

# ---------------------------------------------------------------------------
section("Java: a Paper plugin built with Gradle")
proj = tempfile.mkdtemp()
P = lambda *a: os.path.join(proj, *a)
os.makedirs(P("src", "main", "java", "com", "example"))
os.makedirs(P("src", "main", "resources"))
os.makedirs(P("gradle", "wrapper"))
open(P("settings.gradle.kts"), "w").write('rootProject.name = "shop"\n')
open(P("build.gradle.kts"), "w").write('''plugins {
    java
    id("com.gradleup.shadow") version "8.3.5"
}
repositories {
    mavenCentral()
    maven("https://repo.papermc.io/repository/maven-public/")
}
dependencies {
    implementation("io.papermc.paper:paper-api:1.21.4-R0.1-SNAPSHOT")
    implementation("com.google.code.gson:gson:2.11.0")
}
java {
    toolchain.languageVersion.set(JavaLanguageVersion.of(17))
}
''')
open(P("gradlew"), "w").write("#!/bin/sh\n")
open(P("gradle", "wrapper", "gradle-wrapper.properties"), "w").write(
    "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.10.2-bin.zip\n")
open(P("src", "main", "resources", "plugin.yml"), "w").write("name: Shop\nversion: 1.0\nmain: com.example.Missing\napi-version: '1.21'\n")
open(P("src", "main", "java", "com", "example", "Shop.java"), "w").write("package com.example; public class Shop {}\n")
jp = dt.java_project(proj)
check("Gradle is recognised", jp["kind"] == "gradle" and jp["wrapper"], jp)
check("the wrapper version is read", jp["gradle_version"] == "8.10.2", jp.get("gradle_version"))
check("dependencies are listed", any(d["coord"].startswith("io.papermc.paper:paper-api") for d in jp["dependencies"]), jp["dependencies"])
check("the loader and Minecraft version come from paper-api", jp["loader"] == "paper" and jp["mc"] == "1.21.4", (jp["loader"], jp["mc"]))
check("the toolchain is read", jp["java"] == 17, jp["java"])
check("the build command uses the wrapper", jp["build_command"][-1] == "build" and "gradlew" in jp["build_command"][0])
pm = [p["message"] for p in jp["problems"]]
check("paper-api as implementation is flagged", any("packed into your jar" in m for m in pm), pm)
check("Java 17 for 1.21.4 is flagged", any("needs Java 21" in m for m in pm), pm)
check("a main class that does not exist is flagged", any("com.example.Missing" in m for m in pm), pm)
check("gson as implementation is not flagged", not any("gson" in m for m in pm), pm)
shutil.rmtree(proj)

proj = tempfile.mkdtemp()
open(os.path.join(proj, "build.gradle"), "w").write(
    "plugins { id 'java' }\ndependencies { implementation 'io.papermc.paper:paper-api:1.21.4-R0.1-SNAPSHOT' }\n")
jp = dt.java_project(proj)
check("a dependency on the same line as 'dependencies {' is read (Groovy)",
      any(d["coord"].startswith("io.papermc.paper:paper-api") and d["scope"] == "implementation"
          for d in jp["dependencies"]), jp["dependencies"])
shutil.rmtree(proj)

section("Java: a Maven project")
proj = tempfile.mkdtemp()
open(os.path.join(proj, "pom.xml"), "w").write('''<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <artifactId>coolplugin</artifactId>
  <properties><java.version>21</java.version><spigot.version>1.21.1-R0.1-SNAPSHOT</spigot.version></properties>
  <repositories><repository><url>https://hub.spigotmc.org/nexus/content/repositories/snapshots/</url></repository></repositories>
  <dependencies>
    <dependency><groupId>org.spigotmc</groupId><artifactId>spigot-api</artifactId><version>${spigot.version}</version><scope>provided</scope></dependency>
  </dependencies>
  <build><plugins><plugin><artifactId>maven-shade-plugin</artifactId><version>3.6.0</version></plugin></plugins></build>
</project>''')
jp = dt.java_project(proj)
check("Maven is recognised", jp["kind"] == "maven" and jp["name"] == "coolplugin", jp)
check("properties are expanded in versions", any(d["coord"] == "org.spigotmc:spigot-api:1.21.1-R0.1-SNAPSHOT" for d in jp["dependencies"]), jp["dependencies"])
check("java.version is read", jp["java"] == 21, jp["java"])
check("provided scope is not flagged", not jp["problems"], jp["problems"])
check("plugins and repositories are listed", jp["plugins"][0]["id"] == "maven-shade-plugin" and jp["repositories"], jp)
shutil.rmtree(proj)

section("build output")
JAVAC = """> Task :compileJava FAILED
/home/me/shop/src/main/java/com/example/Shop.java:14: error: cannot find symbol
        Bukkit.getPluginManager().registerEvents(new Listenr(), this);
                                                     ^
  symbol:   class Listenr
  location: class Shop
/home/me/shop/src/main/java/com/example/Shop.java:20: warning: [deprecation] getItemInHand() in PlayerInventory has been deprecated
1 error
1 warning

FAILURE: Build failed with an exception.
"""
b = dt.parse_build_output(JAVAC, "/home/me/shop")
check("a javac error is read with file and line", b["errors"] and b["errors"][0]["line"] == 14
      and b["errors"][0]["file"].endswith("Shop.java"), b["errors"])
check("with symbol and location", "Listenr" in b["errors"][0].get("detail", ""), b["errors"])
check("the warning is separate", len(b["warnings"]) == 1, b["warnings"])
check("the build is failed", b["failed"] and b["summary"].startswith("build failed"), b["summary"])
check("a hint explains cannot find symbol", any("cannot find symbol" in h for h in b["hints"]), b["hints"])
MVN = "[ERROR] /p/src/main/java/a/B.java:[12,5] package org.bukkit does not exist\n[ERROR] BUILD FAILURE\n"
b = dt.parse_build_output(MVN)
check("a Maven error is read", b["errors"] and b["errors"][0]["line"] == 12 and b["errors"][0]["col"] == 5, b)
check("a missing package gets a hint", any("dependency is missing" in h for h in b["hints"]), b["hints"])
KT = "e: file:///C:/p/src/main/kotlin/A.kt:3:9 Unresolved reference 'foo'.\nw: /p/B.kt: (4, 1): unused\n"
b = dt.parse_build_output(KT)
check("Kotlin errors and warnings are read", len(b["errors"]) == 1 and b["errors"][0]["line"] == 3
      and len(b["warnings"]) == 1 and b["warnings"][0]["line"] == 4, b)
b = dt.parse_build_output("Could not find io.papermc.paper:paper-api:1.99-R0.1-SNAPSHOT.\nBUILD FAILED in 3s\n")
check("a missing artifact gets a repository hint", b["failed"] and any("repositories" in h for h in b["hints"]), b)

section("stack traces")
TRACE = """[14:02:11 ERROR]: Could not pass event PlayerJoinEvent to Shop v1.0
org.bukkit.event.EventException: null
\tat org.bukkit.plugin.java.JavaPluginLoader$1.execute(JavaPluginLoader.java:310) ~[paper-api-1.21.4.jar:?]
\tat co.aikar.timings.TimedEventExecutor.execute(TimedEventExecutor.java:80) ~[paper-api-1.21.4.jar:?]
\t... 12 more
Caused by: java.lang.NullPointerException: Cannot invoke "org.bukkit.entity.Player.getName()" because "target" is null
\tat com.example.shop.JoinListener.onJoin(JoinListener.java:27) ~[Shop-1.0.jar:?]
\tat jdk.internal.reflect.GeneratedMethodAccessor1.invoke(Unknown Source) ~[?:?]
\t... 15 more
[14:02:12 INFO]: Steve joined the game
"""
tr = dt.parse_stacktraces(TRACE, packages=["com.example"])
check("one chain is found", len(tr) == 1, tr)
t = tr[0]
check("the top exception and its context", t["exception"] == "org.bukkit.event.EventException"
      and "Could not pass event" in t["context"], t)
check("the root cause is the NPE with Java's message", t["root_cause"]["exception"] == "java.lang.NullPointerException"
      and "because \"target\" is null" in t["root_cause"]["message"], t["root_cause"])
check("the project's own frame is picked out", t["own_frames"] and t["own_frames"][0]["method"] == "onJoin"
      and t["own_frames"][0]["line"] == 27, t["own_frames"])
check("the hint repeats what was null", any("target" in h for h in t["hints"]), t["hints"])
UCV = ("java.lang.UnsupportedClassVersionError: com/example/Shop has been compiled by a more recent version of the "
       "Java Runtime (class file version 65.0), this version of the Java Runtime only recognizes class file "
       "versions up to 61.0\n\tat java.lang.ClassLoader.defineClass1(Native Method)\n")
tr = dt.parse_stacktraces(UCV)
check("class file versions become Java versions", tr and any("Java 21" in h and "Java 17" in h for h in tr[0]["hints"]), tr)
ASYNC = "java.lang.IllegalStateException: Asynchronous player kick!\n\tat org.spigotmc.AsyncCatcher.catchOp(AsyncCatcher.java:15)\n"
tr = dt.parse_stacktraces(ASYNC)
check("an async catcher error is explained", tr and any("main thread" in h for h in tr[0]["hints"]), tr)
MIX = ("org.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError: An unexpected critical error was "
       "encountered\nCaused by: org.spongepowered.asm.mixin.throwables.MixinApplyError: Mixin [coolmod.mixins.json:"
       "MixinWorld] from phase [DEFAULT] in config [coolmod.mixins.json] FAILED during APPLY\n")
tr = dt.parse_stacktraces(MIX)
check("a mixin failure names the mod's mixin config", tr and any("coolmod.mixins.json" in h for h in tr[0]["hints"]), tr)
check("ordinary log lines are not traces", dt.parse_stacktraces("[12:00] [Server thread/INFO]: Done (3.2s)!\n") == [])

section("Java heuristics")
SRC = '''package a;
import org.bukkit.event.EventHandler;
public class L implements Listener {
    // Thread.sleep(5) in a comment is fine
    @EventHandler
    public void onJoin(PlayerJoinEvent e) {
        String s = "Thread.sleep(1)";
        try { Thread.sleep(200); } catch (Exception x) {}
        URL u = new URL("https://x"); u.openConnection();
    }
    public void tick() { Bukkit.getScheduler().runTaskTimer(plugin, () -> {}, 0L, 1L); }
}
'''
lint = dt.java_lint("L.java", SRC)
lines = sorted(p["line"] for p in lint["problems"] if p["severity"] == "warning")
check("blocking calls inside an event handler are found, comments and strings are not", lines == [8, 9], lint["problems"])
check("an every-tick timer is noted", any(p["line"] == 11 and p["severity"] == "info" for p in lint["problems"]), lint["problems"])

section("validate() dispatch")
check("by extension: yml", dt.validate("config.yml", "a: 1\n")["format"] == "yaml")
check("by extension: sk", dt.validate("x.sk", "on join:\n    stop\n")["format"] == "skript")
check("by extension: properties", dt.validate("server.properties", "pvp=maybe\n", bool_keys={"pvp"})["errors"] == 1)
check("unknown files say so", dt.validate("x.png", "")["summary"] == "no checker for this kind of file")
r = dt.validate("plugin.yml", "name: ${project.name}\nversion: @version@\nmain: a.B\napi-version: '1.21'\n")
check("build placeholders in a source plugin.yml are info, not errors", r["ok"] and r["errors"] == 0
      and sum(1 for p in r["problems"] if p["severity"] == "info") == 2, r["problems"])
r = dt.validate("plugins/Skript/config.sk", "language: english\ncheck for new version: true\n")
check("Skript's config.sk is read as settings, not a script", r["ok"] and not r["problems"], r["problems"])

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
