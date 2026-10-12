using System.Text.Json;

namespace Lodestone.Desktop.Core;

/// <summary>
/// Per-machine window preferences - text sizes, wrap, window size. The old page
/// kept these in browser localStorage; the engine's config is for servers, so
/// these live beside the app's own log instead.
/// </summary>
public sealed class Prefs
{
    public static string Dir { get; } = ResolveDir();

    // GetFolderPath answers "" when it cannot resolve the folder, and a bare
    // "Lodestone" would then land in whatever the working directory is - so
    // fall back to the profile, then temp, and always hand out a full path.
    static string ResolveDir()
    {
        var root = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData, Environment.SpecialFolderOption.Create);
        if (string.IsNullOrEmpty(root)) root = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        if (string.IsNullOrEmpty(root)) root = Path.GetTempPath();
        return Path.GetFullPath(Path.Combine(root, "Lodestone"));
    }

    static string FilePath => Path.Combine(Dir, "desktop.json");

    public int Mono { get; set; } = 13;
    public int Ui { get; set; } = 14;
    public bool Wrap { get; set; }
    public double Width { get; set; } = 1320;
    public double Height { get; set; } = 840;
    public bool Maximized { get; set; }
    public bool Intro { get; set; } = true;

    // sound effects
    public bool SoundOn { get; set; } = true;
    public int SoundVolume { get; set; } = 60;
    public bool SoundUi { get; set; } = true;
    public bool SoundNotify { get; set; } = true;
    public bool SoundServer { get; set; } = true;
    public bool SoundPlayers { get; set; } = true;
    public bool SoundChat { get; set; } = true;

    // How the installed mods list was last sorted.
    public string ContentSort { get; set; } = "name";

    // The workspace the assistant and the code editor last had open.
    public string DevWorkspace { get; set; } = "";

    public static Prefs Load()
    {
        try
        {
            if (File.Exists(FilePath))
            {
                var p = JsonSerializer.Deserialize<Prefs>(File.ReadAllText(FilePath));
                if (p != null)
                {
                    p.Mono = Math.Clamp(p.Mono, 10, 28);
                    p.Ui = Math.Clamp(p.Ui, 12, 22);
                    return p;
                }
            }
        }
        catch (Exception)
        {
            // a damaged file just means defaults
        }
        return new Prefs();
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(Dir);
            File.WriteAllText(FilePath, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception)
        {
            // preferences are a convenience; never fail on them
        }
    }
}

/// <summary>Command-line switches.</summary>
public sealed class Options
{
    public int? Port { get; set; }
    public string? EnginePath { get; set; }
    public bool AttachOnly { get; set; }
    public string? ShotsDir { get; set; }

    public static Options Parse(string[] args)
    {
        var o = new Options();
        for (var i = 0; i < args.Length; i++)
        {
            var a = args[i];
            string? Next() => i + 1 < args.Length ? args[++i] : null;
            switch (a)
            {
                case "--port" when int.TryParse(Next(), out var p):
                    o.Port = p;
                    break;
                case "--engine":
                    o.EnginePath = Next();
                    break;
                case "--attach-only":
                    o.AttachOnly = true;
                    break;
                case "--shots":
                    o.ShotsDir = Next();
                    break;
            }
        }
        return o;
    }
}
