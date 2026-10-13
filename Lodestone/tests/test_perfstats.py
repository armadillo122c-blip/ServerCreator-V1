"""Process, host and tick measurements: real processes on this machine, and
each server's own TPS/MSPT output in the exact formats it prints."""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import perfstats as ps

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


section("a real process tree")
if ps.supported():
    # a shell that starts a busy child - the shape of Forge's run script and its JVM
    p = subprocess.Popen([sys.executable, "-c",
                          "import subprocess,sys,time; c=subprocess.Popen([sys.executable,'-c',"
                          "'x=0\\nwhile True: x+=1']); time.sleep(30)"])
    time.sleep(1.0)
    tree = ps.process_tree(p.pid)
    check("the child is found under the parent", len(tree) >= 2 and tree[0] == p.pid, tree)
    s = ps.Sampler()
    u1 = ps.process_usage(tree)
    check("memory and CPU time are read", u1 and u1["rss"] > 1_000_000 and u1["cpu_s"] >= 0, u1)
    s.percent("srv", u1["cpu_s"], 1)
    time.sleep(1.0)
    u2 = ps.process_usage(ps.process_tree(p.pid))
    pct = s.percent("srv", u2["cpu_s"], 1)
    check("CPU percent comes from two readings and sees the busy child", pct is not None and pct > 30, pct)
    check("the first reading of a new key has no percent", ps.Sampler().percent("x", 1.0) is None)
    p.kill()
    for c in tree[1:]:
        try:
            os.kill(c, 9)
        except OSError:
            pass
    p.wait()
    check("a process that is gone gives nothing, not zeros", ps.process_usage([p.pid]) is None)
else:
    print("  note this platform has no process measurement")

section("the host")
s = ps.Sampler()
h = ps.host(s)
time.sleep(0.3)
h = ps.host(s)
if ps.supported():
    check("total and available memory", h["memory_total"] and h["memory_available"]
          and h["memory_available"] <= h["memory_total"], h)
    check("CPU percent after two readings", h["cpu_percent"] is not None and 0 <= h["cpu_percent"] <= 100, h)
check("cores", h["cores"] >= 1)
g = ps.gpus()
check("GPUs either listed or explained", g["available"] or g["why"], g)

section("memory settings")
check("4G is 4096 MB", ps.ram_to_mb("4G") == 4096)
check("6144M is 6144 MB", ps.ram_to_mb("6144M") == 6144)
check("a bare number is MB", ps.ram_to_mb("2048") == 2048)
check("blank is unknown", ps.ram_to_mb("") is None)

section("which command asks each server")
check("Paper: tps and mspt", ps.tps_commands("paper", "1.21.4") == ["tps", "mspt"])
check("Purpur too", ps.tps_commands("purpur", "1.21") == ["tps", "mspt"])
check("Spigot: tps only", ps.tps_commands("spigot", "1.21") == ["tps"])
check("Forge", ps.tps_commands("forge", "1.20.1") == ["forge tps"])
check("NeoForge", ps.tps_commands("neoforge", "1.21.1") == ["neoforge tps"])
check("Fabric on 1.21: tick query", ps.tps_commands("fabric", "1.21.1") == ["tick query"])
check("vanilla before 1.20.3 has nothing", ps.tps_commands("vanilla", "1.20.1") == [])
check("unknown software has nothing", ps.tps_commands("", "1.21") == [])

section("reading the answers")
paper = ["[12:00:00 INFO]: TPS from last 1m, 5m, 15m: 19.8, 20.0, 20.0",
         "[12:00:00 INFO]: Server tick times (avg/min/max) from last 5s, 10s, 1m:",
         "[12:00:00 INFO]: ◴ 12.3/8.1/30.2, 11.9/7.7/30.2, 14.0/6.5/55.1"]
r = ps.parse_tps(paper)
check("Paper's tps line", r.get("tps") == {"1m": 19.8, "5m": 20.0, "15m": 20.0}, r)
check("Paper's mspt lines", r.get("mspt", {}).get("1m") == {"avg": 14.0, "min": 6.5, "max": 55.1}, r)
r = ps.parse_tps(["[12:00:00 INFO]: §6TPS from last 1m, 5m, 15m: §a*20.0, §a20.0, §a20.0"])
check("Spigot's star and colour codes", r.get("tps", {}).get("1m") == 20.0, r)
r = ps.parse_tps(["[12:00:00] [Server thread/INFO] [minecraft/MinecraftServer]: Overall: Mean tick time: 2.345 ms. Mean TPS: 20.000"])
check("Forge's summary", r.get("tps") == {"mean": 20.0} and r["mspt"]["mean"]["avg"] == 2.345, r)
r = ps.parse_tps(["[12:00:00] [Server thread/INFO]: Overall: 19.62 TPS (50.97 ms/tick)"])
check("NeoForge's summary", r.get("tps") == {"mean": 19.62} and r["mspt"]["mean"]["avg"] == 50.97, r)
r = ps.parse_tps(["[12:00:00] [Server thread/INFO]: The game is running normally",
                  "[12:00:00] [Server thread/INFO]: Target tick rate: 20.0 per second.",
                  "Average time per tick: 3.4ms (Target: 50.0ms)",
                  "[12:00:00] [Server thread/INFO]: Percentiles: P50: 3.1ms P95: 5.0ms P99: 9.8ms, sample: 100"])
check("vanilla's tick query: keeping up means the target rate", r.get("tps") == {"mean": 20.0} and r.get("derived"), r)
check("with the percentiles", r["mspt"]["percentiles"] == {"p50": 3.1, "p95": 5.0, "p99": 9.8}, r)
r = ps.parse_tps(["Target tick rate: 20.0 per second.", "Average time per tick: 80.0ms (Target: 50.0ms)"])
check("a lagging vanilla server: 1000/avg", r.get("tps") == {"mean": 12.5}, r)
check("ordinary lines give nothing", ps.parse_tps(["[12:00:00 INFO]: Steve joined the game"]) == {})

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
