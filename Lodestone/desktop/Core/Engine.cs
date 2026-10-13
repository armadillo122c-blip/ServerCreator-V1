using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Lodestone.Desktop.Core;

/// <summary>
/// Finds or starts the Python engine that does Lodestone's real work.
///
/// Order: a Lodestone already answering on the port is adopted as-is (it may
/// be running a Minecraft server, and must not be restarted); otherwise a
/// bundled engine build is started, and failing that lodestone.py with
/// whatever Python is installed. The engine is started with --headless, so it
/// serves its API without opening a browser window.
/// </summary>
public sealed class Engine
{
    public int Port { get; private set; } = 8744;

    /// <summary>True when this app started the engine, false when it adopted one.</summary>
    public bool Owned { get; private set; }

    /// <summary>How it was found, for the Settings page.</summary>
    public string How { get; private set; } = "";

    public string LogPath { get; } = Path.Combine(Prefs.Dir, "engine.log");

    /// <summary>The log of the engine before this one, kept so a crash can still be read after a restart.</summary>
    public string PrevLogPath { get; } = Path.Combine(Prefs.Dir, "engine.prev.log");

    Process? _proc;

    /// <summary>The exit code of the engine this app started, once it has exited.</summary>
    public int? ExitCode
    {
        get
        {
            try { return _proc is { HasExited: true } p ? p.ExitCode : null; }
            catch (InvalidOperationException) { return null; }
        }
    }

    public static int ConfiguredPort()
    {
        try
        {
            var cfg = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".lodestone.json");
            if (File.Exists(cfg))
            {
                var j = JsonNode.Parse(File.ReadAllText(cfg));
                var p = j.I("port");
                if (p is >= 1024 and <= 65535) return p;
            }
        }
        catch (Exception)
        {
            // unreadable config: the engine will say why when it starts
        }
        return 8744;
    }

    public static async Task<bool> IsLodestone(int port, int timeoutMs = 2500)
    {
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromMilliseconds(timeoutMs) };
            var text = await http.GetStringAsync($"http://127.0.0.1:{port}/api/me");
            var j = JsonNode.Parse(text);
            return j.S("app") == "Lodestone" || (j.Has("backups") && j.Has("config"));
        }
        catch (Exception)
        {
            return false;
        }
    }

    public async Task Connect(Options opt, IProgress<string> say)
    {
        Port = opt.Port ?? ConfiguredPort();
        Api.Base = $"http://127.0.0.1:{Port}";

        say.Report("Looking for a running Lodestone…");
        if (await IsLodestone(Port))
        {
            Owned = false;
            How = "adopted the engine already running on port " + Port;
            return;
        }
        if (opt.AttachOnly)
            throw new EngineException($"Nothing is answering on port {Port}, and --attach-only was given.", null);

        var (file, args, label) = Locate(opt.EnginePath);
        say.Report("Starting the engine (" + label + ")…");
        Directory.CreateDirectory(Prefs.Dir);
        KeepPreviousLog();

        var psi = new ProcessStartInfo(file)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = WorkDirFor(args, file),
        };
        foreach (var a in args) psi.ArgumentList.Add(a);
        psi.ArgumentList.Add("--headless");
        psi.ArgumentList.Add(Port.ToString());
        psi.ArgumentList.Add("--log");
        psi.ArgumentList.Add(LogPath);
        // The engine raises a Windows message box when it cannot bind the port.
        // Here the app reports that itself, from the log.
        psi.Environment["LODESTONE_NO_DIALOG"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";

        try
        {
            _proc = Process.Start(psi) ?? throw new EngineException("The engine did not start.", null);
        }
        catch (System.ComponentModel.Win32Exception e)
        {
            throw new EngineException("Could not run " + file + ": " + e.Message, null);
        }
        Owned = true;
        How = "started " + label;

        var until = DateTime.UtcNow.AddSeconds(30);
        while (DateTime.UtcNow < until)
        {
            if (_proc.HasExited)
                throw new EngineException("The engine stopped while starting (exit code " + _proc.ExitCode + ").", LogTail());
            if (await IsLodestone(Port, 1000)) return;
            await Task.Delay(300);
        }
        throw new EngineException("The engine started but never answered on port " + Port + ".", LogTail());
    }

    void KeepPreviousLog()
    {
        try
        {
            if (File.Exists(LogPath) && new FileInfo(LogPath).Length > 0) File.Copy(LogPath, PrevLogPath, true);
            File.WriteAllText(LogPath, "");
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    static string WorkDirFor(List<string> args, string file)
    {
        var script = args.FirstOrDefault(a => a.EndsWith(".py", StringComparison.OrdinalIgnoreCase));
        return Path.GetDirectoryName(script ?? file) ?? Environment.CurrentDirectory;
    }

    public string LogTail(int lines = 25, string? path = null)
    {
        try
        {
            var all = File.ReadAllLines(path ?? LogPath);
            return string.Join("\n", all.Skip(Math.Max(0, all.Length - lines)));
        }
        catch (Exception)
        {
            return "";
        }
    }

    /// <summary>Ask the engine to shut down. Returns the reply, or null if it was unreachable.</summary>
    public async Task<JsonNode?> Quit(bool force)
    {
        try
        {
            return await Api.Post("/api/quit", new { force });
        }
        catch (ApiException e) when (e.Body != null)
        {
            return e.Body;
        }
        catch (ApiException)
        {
            return null;
        }
    }

    // ------------------------------------------------------------------ locate

    static readonly bool Win = RuntimeInformation.IsOSPlatform(OSPlatform.Windows);

    (string file, List<string> args, string label) Locate(string? explicitPath)
    {
        var env = Environment.GetEnvironmentVariable("LODESTONE_ENGINE");
        foreach (var p in new[] { explicitPath, env })
        {
            if (string.IsNullOrWhiteSpace(p)) continue;
            if (!File.Exists(p)) throw new EngineException("The engine given does not exist: " + p, null);
            return p.EndsWith(".py", StringComparison.OrdinalIgnoreCase)
                ? WithPython(p)
                : (p, new List<string>(), Path.GetFileName(p));
        }

        var here = AppContext.BaseDirectory;
        var exeName = Win ? "Lodestone-Engine.exe" : "Lodestone-Engine";
        foreach (var dir in new[] { Path.Combine(here, "engine"), here })
        {
            var exe = Path.Combine(dir, exeName);
            if (File.Exists(exe)) return (exe, new List<string>(), "bundled engine");
        }

        // Source layouts: engine/ beside the app when shipped, and the
        // repository itself when run from desktop/bin/... during development.
        var probe = new DirectoryInfo(here);
        for (var i = 0; i < 7 && probe != null; i++, probe = probe.Parent)
        {
            foreach (var cand in new[] { Path.Combine(probe.FullName, "engine", "lodestone.py"),
                                         Path.Combine(probe.FullName, "lodestone.py") })
            {
                if (File.Exists(cand)) return WithPython(cand);
            }
        }
        throw new EngineException(
            "Could not find the Lodestone engine. Put lodestone.py (and its networking folder) in an " +
            "\"engine\" folder next to this app, or start it with --engine <path>.", null);
    }

    static (string, List<string>, string) WithPython(string script)
    {
        var py = FindPython();
        if (py == null)
            throw new EngineException(
                "Python 3.8 or newer is needed to run the engine, and none was found on PATH. " +
                "Install it from python.org (tick \"Add to PATH\"), then open Lodestone again.", null);
        var file = py.Value.file;
        var args = new List<string>();
        var name = Path.GetFileNameWithoutExtension(file).ToLowerInvariant();
        if (name is "py" or "pyw") args.Add("-3");
        args.Add(script);
        return (file, args, Path.GetFileName(file) + " " + Path.GetFileName(script));
    }

    static (string file, bool windowless)? FindPython()
    {
        // Windowless launchers first so no console flashes up; the Store's
        // python.exe alias opens the Store instead of running, so it is last.
        var names = Win
            ? new[] { "pyw.exe", "pythonw.exe", "py.exe", "python.exe", "python3.exe" }
            : new[] { "python3", "python" };
        var dirs = (Environment.GetEnvironmentVariable("PATH") ?? "")
            .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries).ToList();
        if (Win) dirs.Add(Environment.GetFolderPath(Environment.SpecialFolder.Windows));
        string? storeStub = null;
        foreach (var n in names)
        {
            foreach (var d in dirs)
            {
                string f;
                try { f = Path.Combine(d.Trim('"'), n); }
                catch (ArgumentException) { continue; }
                if (!File.Exists(f)) continue;
                if (Win && f.Contains("WindowsApps", StringComparison.OrdinalIgnoreCase))
                {
                    storeStub ??= f;
                    continue;
                }
                return (f, n.Contains('w'));
            }
        }
        return storeStub == null ? null : (storeStub, false);
    }
}

public sealed class EngineException : Exception
{
    public string? Log { get; }

    public EngineException(string message, string? log) : base(message) => Log = log;
}
