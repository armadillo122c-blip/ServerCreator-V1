"""Measuring a server and the machine it runs on - and saying plainly when
something cannot be measured.

Every number here comes from the operating system or from the Minecraft
server itself; nothing is estimated and shown as if it were measured:

* **process CPU and memory** - the process the panel started *and its
  children*, because Forge and NeoForge start through a script whose child is
  the real JVM. Linux reads ``/proc``; Windows asks the kernel (Toolhelp,
  GetProcessTimes, GetProcessMemoryInfo); elsewhere it is "unavailable".
* **host memory and CPU** - ``/proc/meminfo`` and ``/proc/stat``, or
  GlobalMemoryStatusEx and GetSystemTimes.
* **GPUs** - NVIDIA only, through ``nvidia-smi`` when it is installed.
* **TPS and MSPT** - only from the server's own commands, read back from its
  console: Paper's ``tps``/``mspt``, Spigot's ``tps``, Forge's ``forge tps``,
  NeoForge's ``neoforge tps``, vanilla's ``tick query`` (1.20.3+). The formats
  are the ones in each project's source and language files.

Standard library only.
"""

import ctypes
import os
import re
import shutil
import subprocess
import sys
import time

IS_WIN = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
#  Linux
# ---------------------------------------------------------------------------

def _linux_stat(pid):
    """(ppid, utime+stime seconds, starttime ticks) from /proc/<pid>/stat."""
    with open("/proc/%d/stat" % pid, "rb") as f:
        data = f.read().decode("utf-8", "replace")
    rest = data[data.rindex(")") + 2:].split()
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    return int(rest[1]), (int(rest[11]) + int(rest[12])) / float(hz), int(rest[19])


def _linux_rss(pid):
    try:
        with open("/proc/%d/status" % pid) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _linux_children(root):
    kids = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            ppid = _linux_stat(int(name))[0]
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(name))
    out, stack = [], [root]
    while stack:
        p = stack.pop()
        for c in kids.get(p, []):
            if c not in out:
                out.append(c)
                stack.append(c)
    return out


def _linux_host():
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            if k in ("MemTotal", "MemAvailable"):
                mem[k] = int(v.split()[0]) * 1024
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return mem.get("MemTotal", 0), mem.get("MemAvailable", 0), sum(vals), idle


# ---------------------------------------------------------------------------
#  Windows
# ---------------------------------------------------------------------------

if IS_WIN:
    from ctypes import wintypes

    class _MEMSTAT(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    class _PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    class _PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
    _k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    _INVALID = wintypes.HANDLE(-1).value
    _QUERY = 0x1000           # PROCESS_QUERY_LIMITED_INFORMATION

    def _ft(ft):
        return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

    def _win_table():
        """{pid: (ppid, exe)} from one Toolhelp snapshot."""
        snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
        if not snap or snap == _INVALID:
            return {}
        out = {}
        try:
            e = _PE32()
            e.dwSize = ctypes.sizeof(_PE32)
            ok = _k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
                ok = _k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            _k32.CloseHandle(snap)
        return out

    def _win_proc(pid):
        """(cpu seconds, working set bytes, creation time) or None."""
        h = _k32.OpenProcess(_QUERY, False, pid)
        if not h:
            return None
        try:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return None
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            rss = pmc.WorkingSetSize if _k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb) else 0
            return _ft(k) + _ft(u), rss, _ft(c)
        finally:
            _k32.CloseHandle(h)

    def _win_children(root):
        table = _win_table()
        made = _win_proc(root)
        root_born = made[2] if made else 0
        kids = {}
        for pid, (ppid, _exe) in table.items():
            kids.setdefault(ppid, []).append(pid)
        out, stack = [], [root]
        while stack:
            p = stack.pop()
            for c in kids.get(p, []):
                info = _win_proc(c)
                # a child born before its parent is a reused pid, not a child
                if c in out or (info and root_born and info[2] < root_born):
                    continue
                out.append(c)
                stack.append(c)
        return out

    def _win_host():
        m = _MEMSTAT()
        m.dwLength = ctypes.sizeof(_MEMSTAT)
        _k32.GlobalMemoryStatusEx(ctypes.byref(m))
        idle, kern, user = (wintypes.FILETIME() for _ in range(3))
        _k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        # kernel time includes idle time
        return m.ullTotalPhys, m.ullAvailPhys, _ft(kern) + _ft(user), _ft(idle)


# ---------------------------------------------------------------------------
#  the public side
# ---------------------------------------------------------------------------

def supported():
    return IS_LINUX or IS_WIN


def process_tree(pid):
    """The pid and every descendant, or [pid] when children cannot be listed."""
    if not pid:
        return []
    try:
        if IS_LINUX:
            return [pid] + _linux_children(pid)
        if IS_WIN:
            return [pid] + _win_children(pid)
    except (OSError, ValueError, AttributeError):
        pass
    return [pid]


def process_usage(pids):
    """{"rss": bytes, "cpu_s": seconds} summed over pids, or None."""
    if not pids or not supported():
        return None
    rss, cpu, seen = 0, 0.0, 0
    for pid in pids:
        try:
            if IS_LINUX:
                _pp, c, _st = _linux_stat(pid)
                cpu += c
                rss += _linux_rss(pid)
                seen += 1
            elif IS_WIN:
                info = _win_proc(pid)
                if info:
                    cpu += info[0]
                    rss += info[1]
                    seen += 1
        except (OSError, ValueError, IndexError):
            continue
    return {"rss": rss, "cpu_s": cpu} if seen else None


class Sampler:
    """CPU percentages need two readings; this keeps the previous one per key."""

    def __init__(self):
        self.prev = {}

    def percent(self, key, cpu_s, cores=1, at=None):
        at = at or time.time()
        prev = self.prev.get(key)
        self.prev[key] = (cpu_s, at)
        if not prev or at <= prev[1]:
            return None
        return max(0.0, (cpu_s - prev[0]) / (at - prev[1]) / max(1, cores) * 100.0)

    def host_percent(self, busy_total, idle):
        prev = self.prev.get("__host__")
        self.prev["__host__"] = (busy_total, idle)
        if not prev:
            return None
        dt = busy_total - prev[0]
        di = idle - prev[1]
        return max(0.0, min(100.0, (dt - di) / dt * 100.0)) if dt > 0 else None


def host(sampler=None):
    """Memory and CPU of the machine. Values the OS will not give are None."""
    out = {"cores": os.cpu_count() or 0, "memory_total": None, "memory_available": None,
           "cpu_percent": None, "platform": sys.platform}
    try:
        if IS_LINUX:
            total, avail, busy, idle = _linux_host()
        elif IS_WIN:
            total, avail, busy, idle = _win_host()
        else:
            return out
        out["memory_total"], out["memory_available"] = total or None, avail or None
        if sampler is not None:
            p = sampler.host_percent(busy, idle)
            out["cpu_percent"] = round(p, 1) if p is not None else None
    except (OSError, ValueError, AttributeError, IndexError):
        pass
    return out


_gpu_cache = {"at": 0, "value": None}


def gpus(max_age=15):
    """NVIDIA GPUs from nvidia-smi, or {"available": False, "why": ...}."""
    if time.time() - _gpu_cache["at"] < max_age and _gpu_cache["value"] is not None:
        return _gpu_cache["value"]
    exe = shutil.which("nvidia-smi")
    if not exe:
        val = {"available": False, "why": "No NVIDIA GPU tools found (nvidia-smi). Other GPUs are not read.",
               "items": []}
    else:
        try:
            r = subprocess.run([exe, "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            items = []
            for line in r.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    def num(x):
                        try:
                            return float(x)
                        except ValueError:
                            return None
                    items.append({"name": parts[0],
                                  "memory_total": int(num(parts[1]) * 1048576) if num(parts[1]) else None,
                                  "memory_used": int(num(parts[2]) * 1048576) if num(parts[2]) is not None else None,
                                  "utilization": num(parts[3])})
            val = {"available": bool(items), "items": items,
                   "why": "" if items else "nvidia-smi found no GPU."}
        except (OSError, subprocess.SubprocessError) as e:
            val = {"available": False, "why": "nvidia-smi failed: %s" % e, "items": []}
    _gpu_cache.update(at=time.time(), value=val)
    return val


def ram_to_mb(spec):
    """'4G' / '4096M' / '4096' (MB) -> 4096, '' -> None."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMgGtT]?)[bB]?\s*", spec or "")
    if not m:
        return None
    n, u = float(m.group(1)), m.group(2).lower()
    return int(n * {"k": 1 / 1024, "": 1, "m": 1, "g": 1024, "t": 1048576}[u])


# ---------------------------------------------------------------------------
#  TPS and MSPT, from the server's own commands
# ---------------------------------------------------------------------------

PAPER_LIKE = {"paper", "purpur", "folia", "pufferfish", "leaf", "gale"}


def _mc_tuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])
    except ValueError:
        return ()


def tps_commands(software, mc=""):
    """The commands that make this server report its own tick rate, or []."""
    sw = (software or "").lower()
    if sw in PAPER_LIKE:
        return ["tps", "mspt"]
    if sw == "spigot":
        return ["tps"]
    if sw == "forge":
        return ["forge tps"]
    if sw == "neoforge":
        return ["neoforge tps"]
    t = _mc_tuple(mc)
    if sw in ("vanilla", "fabric", "quilt", "snapshot") and (t >= (1, 20, 3) or (t and t[0] >= 26)):
        return ["tick query"]
    return []


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CODES = re.compile(r"\u00a7[0-9a-fk-orx]", re.I)
PREFIX = re.compile(r"^\s*(?:\[[^\]]*\]:?\s*)+")
NUM = r"\*?(\d+(?:[.,]\d+)?)"


def _clean(line):
    s = CODES.sub("", ANSI.sub("", line or ""))
    return PREFIX.sub("", s).strip() if s.lstrip().startswith("[") else s.strip()


def _f(x):
    return float(x.replace(",", "."))


def parse_tps(lines):
    """Whatever tick numbers the lines contain: {"tps", "mspt", "source"} with
    the keys that were found, or {} when nothing was."""
    out = {}
    lines = [_clean(x) for x in lines]
    for i, s in enumerate(lines):
        m = re.search(r"TPS from last 1m, 5m, 15m:\s*%s,\s*%s,\s*%s" % (NUM, NUM, NUM), s)
        if m:
            out["tps"] = {"1m": _f(m.group(1)), "5m": _f(m.group(2)), "15m": _f(m.group(3))}
            out["source"] = "tps command"
            continue
        if s.startswith("Server tick times") and i + 1 < len(lines):
            nxt = lines[i + 1]
            trip = re.findall(r"(\d+(?:[.,]\d+)?)/(\d+(?:[.,]\d+)?)/(\d+(?:[.,]\d+)?)", nxt)
            if len(trip) >= 3:
                keys = ("5s", "10s", "1m")
                out["mspt"] = {k: {"avg": _f(a), "min": _f(b), "max": _f(c)} for k, (a, b, c) in zip(keys, trip)}
                out["source"] = out.get("source", "mspt command")
            continue
        m = re.search(r"Overall: Mean tick time: (\d+(?:[.,]\d+)?) ms\. Mean TPS: (\d+(?:[.,]\d+)?)", s)
        if m:      # Forge
            out["tps"] = {"mean": _f(m.group(2))}
            out["mspt"] = {"mean": {"avg": _f(m.group(1))}}
            out["source"] = "forge tps"
            continue
        m = re.search(r"Overall: (\d+(?:[.,]\d+)?) TPS \((\d+(?:[.,]\d+)?) ms/tick\)", s)
        if m:      # NeoForge
            out["tps"] = {"mean": _f(m.group(1))}
            out["mspt"] = {"mean": {"avg": _f(m.group(2))}}
            out["source"] = "neoforge tps"
            continue
        m = re.search(r"Target tick rate: (\d+(?:[.,]\d+)?) per second", s)
        if m:      # vanilla /tick query; the next line has the average
            rate = _f(m.group(1))
            rest = " ".join(lines[i:i + 3])
            a = re.search(r"Average time per tick: (\d+(?:[.,]\d+)?)ms", rest)
            if a:
                avg = _f(a.group(1))
                out["mspt"] = {"mean": {"avg": avg}}
                # a server whose ticks take less than their slot runs at the
                # target rate; one that takes longer runs at 1000/avg
                tps = rate if avg <= 1000.0 / rate else 1000.0 / avg
                out["tps"] = {"mean": round(tps, 2)}
                out["source"] = "tick query"
                out["derived"] = "TPS worked out from the average tick time and the target rate"
            p = re.search(r"P50: (\d+(?:[.,]\d+)?)ms P95: (\d+(?:[.,]\d+)?)ms P99: (\d+(?:[.,]\d+)?)ms", rest)
            if p:
                out.setdefault("mspt", {})["percentiles"] = {"p50": _f(p.group(1)), "p95": _f(p.group(2)),
                                                            "p99": _f(p.group(3))}
    return out
