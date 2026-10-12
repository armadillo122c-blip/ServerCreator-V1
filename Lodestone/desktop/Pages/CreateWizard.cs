using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// Create server: pick the Minecraft version and the software that runs it,
/// pick mods or plugins, set the folder, memory, Java and the usual settings,
/// then watch the engine download and install it all. It opens in place of
/// the server list, the way the server editor does.
/// </summary>
public sealed class CreateWizard
{
    readonly MainWindow S;
    readonly Action _close;
    public Control View { get; }

    readonly ContentControl _stepHost = new();
    readonly StackPanel _stepper = Ui.V(0);
    readonly Button _back, _nextBtn;
    readonly TextBlock _footHint = Ui.Hint("");
    int _step;

    // what the engine told us
    JsonNode _catalog = new JsonObject();
    List<JsonNode> _builds = new();
    JsonNode? _java;
    bool _snapshots;

    // choices
    string _mc = "", _software = "", _build = "";
    readonly List<JsonObject> _selection = new();
    readonly TextBox _name = Ui.Input("My server", "My server");
    readonly TextBox _dir = Ui.Input("", "", mono: true);
    bool _dirTouched;
    readonly TextBox _port = Ui.Input("25565", "25565", mono: true);
    readonly TextBox _minRam = Ui.Input("1G", "1G", mono: true);
    readonly TextBox _maxRam = Ui.Input("4G", "4G", mono: true);
    readonly TextBox _extra = Ui.Input("", "optional, space separated", mono: true);
    ComboBox _javaPick = new();
    readonly TextBox _motd = Ui.Input("A Minecraft Server", "A Minecraft Server");
    readonly TextBox _maxPlayers = Ui.Input("20", "20", mono: true);
    readonly TextBox _view = Ui.Input("10", "10", mono: true);
    readonly TextBox _seed = Ui.Input("", "random");
    readonly ComboBox _difficulty = Ui.Select(new[] { ("peaceful", "Peaceful"), ("easy", "Easy"), ("normal", "Normal"), ("hard", "Hard") }, "easy");
    readonly ComboBox _gamemode = Ui.Select(new[] { ("survival", "Survival"), ("creative", "Creative"), ("adventure", "Adventure"), ("spectator", "Spectator") }, "survival");
    readonly ToggleButton _pvp = Ui.Switch("PvP", true);
    readonly ToggleButton _hardcore = Ui.Switch("Hardcore", false);
    readonly ToggleButton _whitelist = Ui.Switch("Whitelist", false);
    readonly ToggleButton _online = Ui.Switch("Online mode", true);
    readonly ToggleButton _cmdBlocks = Ui.Switch("Command blocks", false);
    readonly ToggleButton _eula = Ui.Switch("I accept the Minecraft EULA", false);

    ContentBrowser? _browser;
    readonly StackPanel _selOut = Ui.V(8);
    readonly StackPanel _planOut = Ui.V(8);
    int _planSeq;

    // progress
    string _job = "";

    static readonly string[] StepNames = { "Software", "Mods & plugins", "Settings", "Create" };

    public CreateWizard(MainWindow s, Action close)
    {
        S = s;
        _close = close;
        _back = Ui.Btn("Back", () => Go(_step - 1), "arrow-left", "ghost");
        _nextBtn = Ui.Btn("Next", () => _ = Next(), "chevron-right", "brand");
        _dir.TextChanged += (_, _) => { if (_dir.IsFocused) _dirTouched = true; };
        _name.TextChanged += (_, _) => SuggestDir();

        var head = Ui.Split(
            Ui.H(12, Ui.IconBtn("arrow-left", "Back to instances", () => _close(), "", 38),
                Ui.V(3, Ui.H1("Create a server"),
                    Ui.Hint("Lodestone downloads the server software, the right Java and your mods or plugins, then puts it on the shelf."))),
            Ui.Btn("Cancel", () => _close(), null, "ghost"));
        var foot = Ui.Split(_footHint, Ui.H(8, _back, _nextBtn));
        View = Ui.Scroll(Ui.V(18, head, _stepper, _stepHost, foot), new Thickness(28, 24, 28, 28));
    }

    /// <summary>Make the server in this folder (an empty one the user already picked).</summary>
    public void UseDir(string dir)
    {
        _dirTouched = true;
        _dir.Text = dir;
    }

    // ================================================================ start

    public async Task Start()
    {
        _stepHost.Content = Ui.Card(Ui.Empty("Asking Mojang, PaperMC, Fabric, Forge, NeoForge and the rest what they publish…", "refresh"));
        RenderStepper();
        await LoadCatalog();
        Go(0);
    }

    async Task LoadCatalog()
    {
        try
        {
            _catalog = await Api.Get("/api/create/catalog" + (_snapshots ? "?snapshots=1" : ""));
        }
        catch (Exception e)
        {
            _stepHost.Content = Ui.Card(Ui.V(12, Ui.Empty("Could not load the version list: " + e.Message, "alert"),
                Ui.Btn("Try again", () => _ = Start(), "refresh", "sm").Also(b => b.HorizontalAlignment = HorizontalAlignment.Center)));
            return;
        }
        foreach (var (sw, err) in (_catalog.N("errors") as JsonObject ?? new JsonObject()))
            Toasts.Bad(Name(sw) + " could not be reached: " + J.Str(err));
        var versions = _catalog.A("versions").Items().ToList();
        if (_mc.Length == 0 || versions.All(v => v.S("id") != _mc)) _mc = _catalog.S("latest");
        if (_dir.Text?.Length == 0 || !_dirTouched) SuggestDir();
        var ram = _catalog.L("ram_mb");
        if (ram > 0 && _maxRam.Text == "4G")
            _maxRam.Text = ram >= 16000 ? "6G" : ram >= 8000 ? "4G" : "2G";
        var used = _catalog.A("used_ports").Items().Select(p => int.TryParse(J.Str(p), out var n) ? n : 0).ToHashSet();
        var port = 25565;
        while (used.Contains(port)) port++;
        _port.Text = port.ToString();
    }

    void SuggestDir()
    {
        if (_dirTouched) return;
        var root = _catalog.S("default_dir");
        if (root.Length == 0) return;
        _dir.Text = System.IO.Path.Combine(root, Slug(_name.Text));
    }

    string Name(string sw) => _catalog.A("software").Items().FirstOrDefault(x => x.S("id") == sw).S("name", sw);
    string ContentKind(string sw) => _catalog.A("software").Items().FirstOrDefault(x => x.S("id") == sw).S("content");

    // ================================================================ steps

    void RenderStepper()
    {
        _stepper.Children.Clear();
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        for (var i = 0; i < StepNames.Length; i++)
        {
            var k = i;
            var label = (i + 1) + "  " + (i == 1 && _software.Length > 0
                ? ContentKind(_software) == "mod" ? "Mods" : ContentKind(_software) == "plugin" ? "Plugins" : "Mods & plugins"
                : StepNames[i]);
            var b = Ui.Btn(label, () => { if (k < _step && _job.Length == 0) Go(k); }, i < _step ? "check" : null, "tab");
            b.Classes.Set("active", i == _step);
            b.IsEnabled = i <= _step;
            row.Children.Add(b);
        }
        _stepper.Children.Add(new Border
        {
            Background = Ui.Raised, BorderBrush = Ui.Divider, BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(999), Padding = new Thickness(4), HorizontalAlignment = HorizontalAlignment.Left,
            Child = row,
        });
    }

    void Go(int step)
    {
        if (step < 0)
        {
            _close();
            return;
        }
        _step = Math.Clamp(step, 0, 3);
        RenderStepper();
        _back.IsVisible = _job.Length == 0;
        _nextBtn.IsVisible = _job.Length == 0;
        _back.Content = Ui.H(7, Icons.Make("arrow-left", 17), new TextBlock { Text = _step == 0 ? "Cancel" : "Back", VerticalAlignment = VerticalAlignment.Center });
        _nextBtn.Content = Ui.H(7, Icons.Make(_step == 3 ? "zap" : "chevron-right", 17),
            new TextBlock { Text = _step == 3 ? "Create server" : "Next", VerticalAlignment = VerticalAlignment.Center });
        _footHint.Text = _step switch
        {
            0 => "Only software with a build for the Minecraft version you pick can be chosen.",
            1 => "Dependencies are added automatically, for the exact version you are creating.",
            2 => "Nothing is downloaded until the last step.",
            _ => "",
        };
        _stepHost.Content = _step switch
        {
            0 => SoftwareStep(),
            1 => ContentStep(),
            2 => SettingsStep(),
            _ => ReviewStep(),
        };
    }

    async Task Next()
    {
        switch (_step)
        {
            case 0:
                if (_software.Length == 0)
                {
                    Toasts.Bad("Pick the server software.");
                    return;
                }
                if (_build.Length == 0)
                {
                    Toasts.Bad("Wait for the builds to load, or pick one.");
                    return;
                }
                await LoadJava();
                Go(1);
                break;
            case 1:
                Go(2);
                break;
            case 2:
                var j = await Ops.Act("/api/create/validate", Spec(), null);
                if (j == null) return;
                foreach (var w in j.Strings("warnings")) Toasts.Show(w);
                if (_eula.IsChecked != true)
                    Toasts.Show("The EULA is not ticked: the server will be created, but will not start until you accept it.");
                Go(3);
                break;
            case 3:
                await Create();
                break;
        }
    }

    // ================================================================ 1: software

    Control SoftwareStep()
    {
        var versions = _catalog.A("versions").Items().ToList();
        // Grouped by line - 26.x, 1.21, 1.20 … - so a long list reads like a table of contents.
        var opts = new List<(string, string)>();
        var line = "";
        foreach (var v in versions)
        {
            var l = Line(v.S("id"));
            if (l != line)
            {
                line = l;
                opts.Add(("--" + l, l));
            }
            opts.Add((v.S("id"), v.S("id") + (v.S("type") == "release" ? "" : "  (" + v.S("type") + ")")));
        }
        var mcPick = Ui.Select(opts, _mc, v =>
        {
            _mc = v;
            var cur = versions.FirstOrDefault(x => x.S("id") == v);
            if (_software.Length > 0 && cur != null && !cur.Strings("software").Contains(_software)) _software = "";
            _build = "";
            _selection.Clear();
            Go(0);
        }, 220);
        var snaps = Ui.Switch("show snapshots", _snapshots, on =>
        {
            _snapshots = on;
            _ = Reload();
        });

        var mcInfo = versions.FirstOrDefault(x => x.S("id") == _mc);
        var available = mcInfo.Strings("software");
        var tiles = new UniformGrid { Columns = 4 };
        foreach (var sw in _catalog.A("software").Items())
        {
            var id = sw.S("id");
            var ok = available.Contains(id);
            var kind = sw.S("content") == "mod" ? "Mods" : sw.S("content") == "plugin" ? "Plugins" : "No mods or plugins";
            var t = Ui.Tile(sw.S("name"), (ok ? kind + " · " : "No build for " + _mc + " · ") + sw.S("about"), id == _software);
            // the same tile, with its picture beside the words
            var words = t.Content as Control;
            t.Content = null;
            var art = Images.Art(id, 46, 10);
            if (!ok) art.Opacity = 0.4;
            t.Content = Ui.Lead(art, words!).Also(g => g.Margin = new Thickness(0, 0, 18, 0));
            t.IsEnabled = ok;
            t.Margin = new Thickness(0, 0, 10, 10);
            t.MinHeight = 108;
            t.VerticalContentAlignment = VerticalAlignment.Top;
            t.Click += (_, _) =>
            {
                if (_software != id) _selection.Clear();
                _software = id;
                _build = "";
                Go(0);
            };
            tiles.Children.Add(t);
        }

        var buildCard = new ContentControl();
        if (_software.Length > 0) _ = LoadBuilds(buildCard);

        return Ui.V(16,
            Ui.Card("Minecraft version", "Releases only, unless you ask for snapshots", snaps, Ui.V(10, mcPick,
                Ui.Hint(available.Count + " of 8 server types have a build for " + _mc + "."))),
            Ui.Card("Server software", "What runs the server - and decides whether it takes mods or plugins", null, tiles),
            buildCard);
    }

    /// <summary>"1.21.4" -> "1.21", "26.3" -> "26.x": the heading a version sits under.</summary>
    static string Line(string mc)
    {
        var parts = mc.Split('.', '-');
        if (parts.Length > 0 && int.TryParse(parts[0], out var major) && major >= 26) return major + ".x";
        return parts.Length >= 2 ? parts[0] + "." + parts[1] : mc;
    }

    async Task Reload()
    {
        _stepHost.Content = Ui.Card(Ui.Empty("Loading…", "refresh"));
        await LoadCatalog();
        Go(0);
    }

    async Task LoadBuilds(ContentControl into)
    {
        into.Content = Ui.Card(Ui.Hint("Loading " + Name(_software) + " builds for " + _mc + "…"));
        JsonNode j;
        try
        {
            j = await Api.Get("/api/create/builds?software=" + Api.Q(_software) + "&mc=" + Api.Q(_mc));
        }
        catch (Exception e)
        {
            into.Content = Ui.Card(Ui.T(e.Message, null, 13.5, Ui.Red, wrap: true));
            _build = "";
            return;
        }
        _builds = j.A("items").Items().ToList();
        if (_build.Length == 0 || _builds.All(b => b.S("id") != _build))
            _build = (_builds.FirstOrDefault(b => b.B("recommended")) ?? _builds.FirstOrDefault()).S("id");
        var pick = Ui.Select(_builds.Select(b => (b.S("id"), b.S("label") + (b.B("recommended") ? "  - recommended" : ""))), _build,
            v => _build = v, 340);
        var notes = Ui.V(6, Ui.Hint("Minecraft " + _mc + " needs Java " + j.S("java") + ". Lodestone finds an installed one, or downloads it for you."));
        if (_software == "spigot")
            notes.Children.Add(Ui.T("Spigot is not published as a download - BuildTools compiles it on this PC. That takes several " +
                                    "minutes and needs Git (BuildTools fetches a portable copy on Windows).", null, 12.5, Ui.Orange, wrap: true));
        if (_software is "forge" or "neoforge" or "quilt")
            notes.Children.Add(Ui.Hint(Name(_software) + " is installed by running its official installer, which downloads the game libraries."));
        into.Content = Ui.Card(Name(_software) + " build", null, null, Ui.V(10, pick, notes));
    }

    async Task LoadJava()
    {
        try
        {
            _java = await Api.Get("/api/java?mc=" + Api.Q(_mc) + "&software=" + Api.Q(_software));
        }
        catch (Exception e)
        {
            _java = null;
            Toasts.Bad("Could not look for Java: " + e.Message);
        }
    }

    // ================================================================ 2: mods / plugins

    string Target() => "software=" + Api.Q(_software) + "&mc=" + Api.Q(_mc);

    Control ContentStep()
    {
        var kind = ContentKind(_software);
        if (kind.Length == 0)
            return Ui.Card(Ui.Empty(Name(_software) + " runs no mods or plugins - this step is only for modded and plugin servers. " +
                                    "Press Next.", "puzzle"));
        var noun = kind == "mod" ? "mods" : "plugins";
        var presets = _catalog.N("presets").A(_software).Items().ToList();
        var recBtn = presets.Count > 0
            ? Ui.Btn("Add the recommended set", () => _ = AddPreset(presets), "package", "sm",
                string.Join(", ", presets.Select(p => p.S("name"))))
            : null;

        _browser = new ContentBrowser(Target, Add, "Add", "plus",
            Picked);
        RenderSelection();
        var view = Ui.V(16,
            Ui.Card("Your " + noun, "Installed into " + (kind == "mod" ? "mods/" : "plugins/") + " when the server is created", recBtn,
                Ui.V(10, _selOut, _planOut)),
            Ui.Card("Find " + noun, "Only " + noun + " for " + Name(_software) + " " + _mc + " are offered", null, _browser.View));
        _ = InitBrowser(_browser);
        return view;
    }

    async Task InitBrowser(ContentBrowser b)
    {
        try
        {
            var j = await Api.Get("/api/content/profile?" + Target());
            await b.SetProfile(j.N("profile")!);
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
    }

    // The recommended set names projects by slug, search results by id: either counts.
    bool Picked(JsonNode h) => _selection.Any(x => x.S("source") == h.S("source") &&
                                                   (x.S("project_id") == h.S("id") || x.S("project_id") == h.S("slug")));

    Task Add(JsonNode hit)
    {
        if (Picked(hit)) return Task.CompletedTask;
        _selection.Add(new JsonObject
        {
            ["source"] = hit.S("source"), ["project_id"] = hit.S("id"), ["name"] = hit.S("name"), ["icon"] = hit.S("icon"),
        });
        RenderSelection();
        return Task.CompletedTask;
    }

    async Task AddPreset(List<JsonNode> presets)
    {
        foreach (var p in presets)
            if (!_selection.Any(x => x.S("source") == p.S("source") && x.S("project_id") == p.S("project_id")))
                _selection.Add(new JsonObject { ["source"] = p.S("source"), ["project_id"] = p.S("project_id"), ["name"] = p.S("name") });
        RenderSelection();
        _browser?.Rerender();
        await Task.CompletedTask;
    }

    void RenderSelection()
    {
        _selOut.Children.Clear();
        if (_selection.Count == 0)
        {
            _selOut.Children.Add(Ui.Hint("Nothing yet - add some below, or skip this step. You can always add more later from the " +
                                         "Mods & plugins tab."));
            _planOut.Children.Clear();
            return;
        }
        var wrap = new WrapPanel();
        foreach (var x in _selection.ToList())
        {
            var chip = new Border
            {
                Background = Ui.ButtonBg, CornerRadius = new CornerRadius(999), Padding = new Thickness(4, 4, 6, 4),
                Margin = new Thickness(0, 0, 8, 8),
                Child = Ui.H(6, Images.Project(x.S("icon"), 22, 11),
                    Ui.T(x.S("name"), null, 13, Ui.Contrast, FontWeight.SemiBold),
                    Ui.T(ContentBrowser.SourceName(x.S("source")), null, 11.5, Ui.Secondary),
                    Ui.IconBtn("x", "Remove", () =>
                    {
                        _selection.Remove(x);
                        RenderSelection();
                        _browser?.Rerender();
                    }, "ghost round", 24)),
            };
            wrap.Children.Add(chip);
        }
        _selOut.Children.Add(wrap);
        _ = Preview();
    }

    async Task Preview()
    {
        var seq = ++_planSeq;
        _planOut.Children.Clear();
        _planOut.Children.Add(Ui.Hint("checking versions and dependencies…"));
        JsonNode pl;
        try
        {
            var items = new JsonArray(_selection.Select(x => (JsonNode)new JsonObject
            {
                ["source"] = x.S("source"), ["project_id"] = x.S("project_id"),
            }).ToArray());
            pl = await Api.Post("/api/content/plan", new JsonObject { ["software"] = _software, ["mc"] = _mc, ["items"] = items });
        }
        catch (Exception e)
        {
            if (seq != _planSeq) return;
            _planOut.Children.Clear();
            _planOut.Children.Add(Ui.T(e.Message, null, 12.5, Ui.Red, wrap: true));
            return;
        }
        if (seq != _planSeq) return;
        _planOut.Children.Clear();
        _planOut.Children.Add(Ui.Eyebrow("What will be installed"));
        ContentBrowser.RenderPlan(_planOut, pl);
    }

    // ================================================================ 3: settings

    Control SettingsStep()
    {
        var browse = Ui.Btn("Browse", PickDir, "folder", "sm");
        var dirRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        dirRow.Children.Add(_dir);
        browse.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(browse, 1);
        dirRow.Children.Add(browse);

        var ramMb = _catalog.L("ram_mb");
        var need = _java?.I("need") ?? 0;
        var javaOpts = new List<(string, string)>();
        var pickPath = _java.S("pick");
        javaOpts.Add(("auto", pickPath.Length > 0
            ? "Automatic - Java " + (_java?.A("items").Items().FirstOrDefault(j => j.S("path") == pickPath).S("major")) + " (" + pickPath + ")"
            : "Automatic - download Java " + need + " (none installed fits)"));
        javaOpts.Add(("download", "Download Java " + need + " (Eclipse Temurin) into Lodestone's folder"));
        foreach (var jv in _java?.A("items").Items() ?? Enumerable.Empty<JsonNode>())
            javaOpts.Add((jv.S("path"), "Java " + jv.S("major") + "  ·  " + jv.S("path") + (jv.B("fits") ? "" : "  - " + jv.S("note"))));
        var prevJava = Ui.Val(_javaPick) ?? "auto";
        _javaPick = Ui.Select(javaOpts, prevJava);
        _javaPick.HorizontalAlignment = HorizontalAlignment.Stretch;

        var eulaLink = Ui.Btn("Read the EULA", () => MainWindow.OpenUrl("https://aka.ms/MinecraftEULA"), "external", "ghost sm");
        return Ui.V(16,
            Ui.Card("Server", null, null, Ui.V(0,
                Ui.Cols(16, Ui.Field("Name", _name), Ui.Field("Port", _port, "Each server that runs at the same time needs its own.")),
                Ui.Field("Install folder", dirRow, "A new or empty folder. Nothing that is already there gets overwritten."))),
            Ui.Card("Memory and Java", ramMb > 0 ? "This PC has " + (ramMb / 1024.0).ToString("0.#") + " GB of memory" : null, null, Ui.V(0,
                Ui.Cols(16, Ui.Field("Minimum RAM", _minRam), Ui.Field("Maximum RAM", _maxRam,
                    _software is "forge" or "neoforge" ? "Modpacks want 6-8G; a light one 4G." : "2-4G is plenty for a few friends.")),
                Ui.Field("Java", _javaPick, need > 0 ? "Minecraft " + _mc + " needs Java " + need + "." : null),
                Ui.Field("Extra JVM flags", _extra, "Passed straight to Java, like the Extra JVM flags in a server's settings."))),
            Ui.Card("Gameplay", "Written to server.properties - change any of it later in the Properties tab", null, Ui.V(0,
                Ui.Field("MOTD", _motd, "The line under the name in the server list. & colour codes work."),
                Ui.Cols(16, Ui.Field("Difficulty", _difficulty), Ui.Field("Game mode", _gamemode),
                    Ui.Field("Max players", _maxPlayers), Ui.Field("View distance", _view)),
                Ui.Field("World seed", _seed),
                Ui.Cols(16, Ui.V(0, Ui.SwitchHint(_pvp, null), Ui.SwitchHint(_hardcore, null), Ui.SwitchHint(_cmdBlocks, null)),
                    Ui.V(0, Ui.SwitchHint(_whitelist, "Only players you add can join."),
                        Ui.SwitchHint(_online, "Checks that players own Minecraft. Leave it on; for testing with non-premium " +
                                               "accounts use Offline mode in the Network tab after creating."))))),
            Ui.Card("Minecraft EULA", "Mojang's licence - a server does not start until it is accepted", eulaLink,
                Ui.SwitchHint(_eula, "Lodestone writes eula=true only if you switch this on yourself.")));
    }

    async void PickDir()
    {
        var r = await S.StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title = "Where should the server go?" });
        var p = r.FirstOrDefault()?.TryGetLocalPath();
        if (p == null) return;
        _dirTouched = true;
        // An empty folder is used as it is; anything else gets a new folder inside it.
        var empty = Directory.Exists(p) && !Directory.EnumerateFileSystemEntries(p).Any();
        _dir.Text = empty ? p : System.IO.Path.Combine(p, Slug(_name.Text));
    }

    static string Slug(string? name)
    {
        var slug = new string((name ?? "server").Trim().Select(c => char.IsLetterOrDigit(c) || c is '-' or '_' or '.' ? c : c == ' ' ? '-' : '\0')
            .Where(c => c != '\0').ToArray());
        return slug.Length > 0 ? slug : "server";
    }

    JsonObject Spec()
    {
        var props = new JsonObject
        {
            ["motd"] = _motd.Text ?? "",
            ["max-players"] = _maxPlayers.Text ?? "",
            ["view-distance"] = _view.Text ?? "",
            ["level-seed"] = _seed.Text ?? "",
            ["difficulty"] = Ui.Val(_difficulty),
            ["gamemode"] = Ui.Val(_gamemode),
            ["pvp"] = _pvp.IsChecked == true,
            ["hardcore"] = _hardcore.IsChecked == true,
            ["white-list"] = _whitelist.IsChecked == true,
            ["online-mode"] = _online.IsChecked == true,
            ["enable-command-block"] = _cmdBlocks.IsChecked == true,
        };
        var content = new JsonArray(_selection.Select(x => (JsonNode)new JsonObject
        {
            ["source"] = x.S("source"), ["project_id"] = x.S("project_id"), ["name"] = x.S("name"),
        }).ToArray());
        return new JsonObject
        {
            ["name"] = _name.Text ?? "",
            ["software"] = _software, ["mc"] = _mc, ["build"] = _build,
            ["dir"] = _dir.Text ?? "",
            ["port"] = _port.Text ?? "",
            ["min_ram"] = _minRam.Text ?? "", ["max_ram"] = _maxRam.Text ?? "",
            ["java"] = Ui.Val(_javaPick) ?? "auto",
            ["extra"] = _extra.Text ?? "",
            ["eula"] = _eula.IsChecked == true,
            ["properties"] = props,
            ["content"] = content,
        };
    }

    // ================================================================ 4: review and create

    Control ReviewStep()
    {
        var b = _builds.FirstOrDefault(x => x.S("id") == _build);
        var javaText = (_javaPick.SelectedItem as ComboBoxItem)?.Content as string ?? "Automatic";
        var rows = Ui.V(0,
            Ui.KRow("Software", Name(_software) + " - " + b.S("label", _build)),
            Ui.KRow("Minecraft", _mc),
            Ui.KRow("Folder", _dir.Text),
            Ui.KRow("Java", javaText),
            Ui.KRow("Memory", (_minRam.Text ?? "") + " - " + (_maxRam.Text ?? "")),
            Ui.KRow("Port", _port.Text),
            Ui.KRow(ContentKind(_software) == "mod" ? "Mods" : "Plugins",
                _selection.Count == 0 ? "none" : string.Join(", ", _selection.Select(x => x.S("name")))),
            Ui.KRow("EULA", _eula.IsChecked == true ? "accepted" : "not accepted - accept it before the first start",
                _eula.IsChecked == true ? null : Ui.Orange));
        var hero = Ui.Lead(Images.Art(_software, 72, 16),
            Ui.V(3, Ui.H2("Ready to create " + (_name.Text ?? "the server")),
                Ui.Hint(Name(_software) + " for Minecraft " + _mc + ". Everything below is downloaded and verified before it is used.")));
        return Ui.Card(Ui.V(18, hero, rows));
    }

    async Task Create()
    {
        _nextBtn.IsEnabled = false;
        var j = await Ops.Act("/api/create", Spec(), null);
        _nextBtn.IsEnabled = true;
        if (j == null) return;
        _job = j.S("job");
        _back.IsVisible = _nextBtn.IsVisible = false;
        _footHint.Text = "";
        await Watch();
    }

    async Task Watch()
    {
        var steps = Ui.V(6);
        var bar = new ProgressBar
        {
            Minimum = 0, Maximum = 100, Height = 8, MinHeight = 8, CornerRadius = new CornerRadius(4),
            Foreground = Ui.Brand, Background = Ui.ButtonBg,
        };
        var pct = Ui.Hint("0%");
        var logText = Ui.Selectable("", true, 12);
        var logBox = new ScrollViewer
        {
            MaxHeight = 320, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
            Content = new Border { Padding = new Thickness(12), Child = logText },
        };
        var logCard = new Border { Child = logBox };
        logCard.Classes.Add("code");
        var outcome = Ui.V(10);
        var title = Ui.H2("Creating " + (_name.Text ?? "the server") + "…");
        _stepHost.Content = Ui.V(16,
            Ui.Card(Ui.V(14, Ui.Split(Ui.H(14, Images.Art(_software, 48, 12), title), pct), bar, steps, outcome)),
            Ui.Card("Log", null, null, logCard));

        var lines = new List<string>();
        var since = 0;
        while (true)
        {
            JsonNode job;
            try
            {
                job = (await Api.Get("/api/jobs?id=" + Api.Q(_job) + "&since=" + since)).N("job")!;
            }
            catch (Exception e)
            {
                outcome.Children.Clear();
                outcome.Children.Add(Ui.T("Lost track of the job: " + e.Message, null, 13.5, Ui.Red, wrap: true));
                await Task.Delay(2000);
                continue;
            }
            since = job.I("next");
            foreach (var l in job.Strings("log")) lines.Add(l);
            if (lines.Count > 400) lines.RemoveRange(0, lines.Count - 400);
            logText.Text = string.Join("\n", lines);
            logBox.ScrollToEnd();
            bar.Value = job.D("pct");
            pct.Text = job.S("pct", "0") + "%";
            steps.Children.Clear();
            foreach (var st in job.A("steps").Items())
            {
                var (kind, label) = st.S("state") switch
                {
                    "done" => ("on", "done"),
                    "running" => ("info", "working"),
                    "failed" => ("bad", "failed"),
                    "warn" => ("warn", "with warnings"),
                    _ => ("", "waiting"),
                };
                steps.Children.Add(Ui.Row(Ui.TitleSub(st.S("label"), st.S("detail")), Ui.Pill(label, kind)));
            }
            var state = job.S("state");
            if (state == "running")
            {
                await Task.Delay(800);
                continue;
            }
            outcome.Children.Clear();
            foreach (var w in job.Strings("warnings"))
                outcome.Children.Add(Ui.T(w, null, 13, Ui.Orange, wrap: true));
            if (state == "done")
            {
                title.Text = (_name.Text ?? "The server") + " is ready";
                var sid = job.N("result").S("server_id");
                var start = Ui.Btn("Start it", async () =>
                {
                    _close();
                    await S.OpenInstance(sid, "console");
                    await Ops.Act("/api/start", new { sid }, "Starting.");
                }, "play", "brand");
                start.IsEnabled = _eula.IsChecked == true;
                if (_eula.IsChecked != true) ToolTip.SetTip(start, "Accept the EULA first - the console has a button for it.");
                outcome.Children.Add(Ui.H(8, start,
                    Ui.Btn("Open it", async () =>
                    {
                        _close();
                        await S.OpenInstance(sid, "console");
                    }, "chevron-right", "soft"),
                    Ui.Btn("Back to instances", () => _close(), null, "ghost")));
                Toasts.Good((_name.Text ?? "Server") + " created.");
                await S.LoadShelf();
            }
            else
            {
                title.Text = "Could not create " + (_name.Text ?? "the server");
                outcome.Children.Add(Ui.T(job.S("error"), null, 14, Ui.Red, wrap: true));
                outcome.Children.Add(Ui.Hint("Anything half-made was removed again, so you can change a setting and try once more."));
                outcome.Children.Add(Ui.H(8,
                    Ui.Btn("Change settings", () =>
                    {
                        _job = "";
                        Go(2);
                    }, "edit", "brand"),
                    Ui.Btn("Back to instances", () => _close(), null, "ghost")));
            }
            break;
        }
    }

    /// <summary>For --shots: jump straight to a step with a typical choice made.</summary>
    public async Task ShotStep(int step, string software, string mc)
    {
        await LoadCatalog();
        _software = software;
        _mc = mc;
        _build = "";
        if (step >= 1)
        {
            var j = await Api.Get("/api/create/builds?software=" + Api.Q(_software) + "&mc=" + Api.Q(_mc));
            _builds = j.A("items").Items().ToList();
            _build = (_builds.FirstOrDefault(b => b.B("recommended")) ?? _builds.FirstOrDefault()).S("id");
            await LoadJava();
        }
        Go(step);
        if (step == 1)
        {
            foreach (var p in _catalog.N("presets").A(_software).Items().Take(3))
                _selection.Add(new JsonObject { ["source"] = p.S("source"), ["project_id"] = p.S("project_id"), ["name"] = p.S("name") });
            RenderSelection();
        }
    }
}
