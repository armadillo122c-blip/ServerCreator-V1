using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using AvaloniaEdit;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// The code editor and the developer tools around it, for any workspace - a
/// server's folder or a plugin/mod project. Saving backs the file up (it can
/// be put back from the assistant's "Files changed") and checks it; the
/// problems list jumps to the line. Skript gets Skript's own verdict from the
/// running server; Java projects build with their own Gradle or Maven.
/// </summary>
public sealed class CodePage : Page
{
    public CodePage(MainWindow s) : base(s) { }

    public override string Key => "code";
    public override bool ServerScoped => false;
    public override string Title => "Code";
    public override string Icon => "code";

    JsonNode _state = new JsonObject();
    string _ws = "";
    string _tab = "edit";
    ComboBox _wsBox = null!;
    bool _wsLoading;
    ContentControl _body = null!;
    readonly Dictionary<string, Control> _tabs = new();
    Action<string>? _selectTab;

    // editor
    string _dir = ".";
    StackPanel _tree = null!;
    TextBlock _treePath = null!;
    TextEditor _ed = null!;
    TextBlock _edName = null!;
    Border _edState = null!;
    StackPanel _problems = null!;
    TextBlock _problemsHead = null!;
    string _path = "";
    double _mtime;
    bool _loading;

    // skript / java / trace
    StackPanel _skOut = null!, _javaOut = null!, _buildOut = null!, _traceOut = null!;
    TextBox _traceIn = null!;
    string? _job;

    protected override Control Build()
    {
        _wsBox = new ComboBox { MinWidth = 280 };
        _wsBox.SelectionChanged += (_, _) =>
        {
            if (_wsLoading) return;
            var id = Ui.Val(_wsBox);
            if (id != null && id != _ws) _ = OpenWorkspace(id);
        };
        var (bar, select) = Ui.NavTabs(new (string, string, string?)[]
        {
            ("edit", "Editor", "file-text"), ("skript", "Skript", "puzzle"), ("java", "Java & build", "package"),
            ("trace", "Errors & traces", "alert"),
        }, _tab, k =>
        {
            _tab = k;
            _body.Content = _tabs[k];
            _ = LoadTab();
        });
        _selectTab = select;
        var top = Ui.Split(bar, Ui.H(8, Ui.T("Workspace", null, 12.5, Ui.Secondary), _wsBox));
        top.Margin = new Thickness(0, 0, 0, 14);

        _tabs["edit"] = BuildEditor();
        _tabs["skript"] = Ui.Scroll(Ui.V(16, _skOut = Ui.V(14)), new Thickness(0, 0, 0, 20));
        _javaOut = Ui.V(14);
        _buildOut = Ui.V(10);
        _tabs["java"] = Ui.Scroll(Ui.V(16, _javaOut, _buildOut), new Thickness(0, 0, 0, 20));
        _traceIn = Ui.Area("", 8, "Paste a stack trace, a crash report or build output", true);
        _traceOut = Ui.V(12);
        _tabs["trace"] = Ui.Scroll(Ui.V(16,
            Ui.Card("Explain an error", "The exception chain, the frames that are in this project, and the usual causes - read from the trace itself.",
                Ui.H(8, Ui.Btn("From the server console", () => _ = Trace(true), "console", "sm"),
                    Ui.Btn("Explain", () => _ = Trace(false), "search", "brand sm")),
                _traceIn),
            _traceOut), new Thickness(0, 0, 0, 20));

        _body = new ContentControl { Content = _tabs[_tab] };
        var g = new Grid { RowDefinitions = new RowDefinitions("Auto,*"), Margin = new Thickness(24, 16, 24, 20) };
        g.Children.Add(top);
        Grid.SetRow(_body, 1);
        g.Children.Add(_body);
        return g;
    }

    Control BuildEditor()
    {
        _tree = Ui.V(1);
        _treePath = Ui.T(".", null, 12, Ui.Secondary).Also(t => t.TextTrimming = TextTrimming.CharacterEllipsis);
        var treeHead = Ui.Split(Ui.V(2, Ui.H2("Files"), _treePath),
            Ui.H(2, Ui.IconBtn("arrow-up", "Up one folder", () => _ = List(Parent(_dir)), "ghost sm", 30),
                Ui.IconBtn("refresh", "Refresh", () => _ = List(_dir), "ghost sm", 30)));
        var treeCard = Ui.FillCard(treeHead, new ScrollViewer
        {
            Content = _tree,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            Padding = new Thickness(8, 0, 8, 8),
        });

        _ed = new TextEditor
        {
            ShowLineNumbers = true,
            FontFamily = Ui.Mono,
            FontSize = S.Prefs.Mono,
            Background = Ui.Terminal,
            Foreground = Ui.Hex("#DCE2E8"),
            LineNumbersForeground = Ui.Hex("#5B6370"),
            WordWrap = S.Prefs.Wrap,
            Padding = new Thickness(8),
            IsReadOnly = true,
        };
        _ed.Options.IndentationSize = 4;
        _ed.Options.ConvertTabsToSpaces = false;
        _ed.Options.EnableHyperlinks = false;
        _ed.Options.EnableEmailHyperlinks = false;
        _ed.Options.HighlightCurrentLine = true;
        _ed.TextArea.TextView.CurrentLineBackground = Ui.Hex("#15171B");
        _ed.TextArea.TextView.CurrentLineBorder = new Pen(Ui.Hex("#1D1F23"));
        _ed.TextArea.SelectionBrush = Ui.Hex("#551BD96A");
        _ed.TextArea.SelectionForeground = null;
        _ed.TextArea.Caret.CaretBrush = Ui.Brand;
        _ed.TextChanged += (_, _) =>
        {
            if (!_loading && _path.Length > 0) Ui.SetPill(_edState, "unsaved", "warn");
        };
        _ed.AddHandler(InputElement.KeyDownEvent, (_, e) =>
        {
            if (e.Key == Avalonia.Input.Key.S && e.KeyModifiers.HasFlag(KeyModifiers.Control))
            {
                e.Handled = true;
                _ = Save();
            }
        }, Avalonia.Interactivity.RoutingStrategies.Tunnel);
        _edName = Ui.H2("No file open");
        _edName.TextTrimming = TextTrimming.CharacterEllipsis;
        _edState = Ui.Pill("—", "", false);
        var edHead = Ui.Split(Ui.H(10, Icons.Make("file-text", 20, Ui.Secondary), _edName, _edState),
            Ui.H(6,
                Ui.Btn("Check", () => _ = Check(), "circle-check", "sm", "Validate the file without saving"),
                Ui.Btn("Ask the assistant", AskAboutFile, "sparkles", "sm", "Open the assistant with this file and its problems"),
                Ui.Btn("Save", () => _ = Save(), "save", "brand sm", "Save (Ctrl+S) - backed up first, checked after")));
        var edBody = new Border { Child = _ed, BorderBrush = Ui.Divider, BorderThickness = new Thickness(0, 1, 0, 0) };
        var edCard = Ui.FillCard(edHead, edBody);

        _problemsHead = Ui.T("Problems", "h3");
        _problems = Ui.V(2);
        var probCard = Ui.FillCard(_problemsHead, new ScrollViewer
        {
            Content = _problems,
            Padding = new Thickness(12, 0, 12, 10),
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        });

        var right = new Grid { RowDefinitions = new RowDefinitions("*,8,170") };
        right.Children.Add(edCard);
        var hs = new GridSplitter { Background = Brushes.Transparent, ResizeDirection = GridResizeDirection.Rows };
        Grid.SetRow(hs, 1);
        right.Children.Add(hs);
        Grid.SetRow(probCard, 2);
        right.Children.Add(probCard);

        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("270,8,*") };
        g.Children.Add(treeCard);
        var vs = new GridSplitter { Background = Brushes.Transparent, ResizeDirection = GridResizeDirection.Columns };
        Grid.SetColumn(vs, 1);
        g.Children.Add(vs);
        Grid.SetColumn(right, 2);
        g.Children.Add(right);
        return g;
    }

    static string Parent(string p)
    {
        if (p is "." or "") return ".";
        var i = p.TrimEnd('/').LastIndexOf('/');
        return i <= 0 ? "." : p[..i];
    }

    // ================================================================ workspaces

    public override async Task OnShow()
    {
        await LoadState();
        await LoadTab();
    }

    async Task LoadState()
    {
        var j = await Ops.Get("/api/agent/state");
        if (j == null) return;
        _state = j;
        var wss = j.A("workspaces").Items().Where(w => w.B("exists")).ToList();
        var want = _ws.Length > 0 ? _ws : S.Prefs.DevWorkspace;
        var cur = S.State.Path("server.id") is { } sid ? "srv-" + J.Str(sid) : "";
        if (want.Length == 0 || wss.All(w => w.S("id") != want))
            want = wss.Any(w => w.S("id") == cur) ? cur : wss.FirstOrDefault()?.S("id") ?? "";
        _wsLoading = true;
        _wsBox.Items.Clear();
        foreach (var w in wss)
            _wsBox.Items.Add(new ComboBoxItem { Content = (w.S("kind") == "server" ? "Server · " : "Project · ") + w.S("name"), Tag = w.S("id") });
        Ui.SetVal(_wsBox, want);
        _wsLoading = false;
        if (want != _ws) await OpenWorkspace(want);
    }

    JsonNode? Workspace() => _state.A("workspaces").Items().FirstOrDefault(w => w.S("id") == _ws);

    async Task OpenWorkspace(string id)
    {
        _ws = id;
        S.Prefs.DevWorkspace = id;
        S.Prefs.Save();
        _path = "";
        _loading = true;
        _ed.Text = "";
        _ed.IsReadOnly = true;
        _loading = false;
        _edName.Text = "No file open";
        Ui.SetPill(_edState, "—", "", false);
        _problems.Children.Clear();
        await List(".");
        await LoadTab();
    }

    /// <summary>From the assistant or anywhere else: open a file at a line.</summary>
    public async Task OpenAt(string wsid, string path, int line)
    {
        if (_state.A("workspaces").Count == 0) await LoadState();
        if (wsid.Length > 0 && wsid != _ws)
        {
            _wsLoading = true;
            Ui.SetVal(_wsBox, wsid);
            _wsLoading = false;
            await OpenWorkspace(wsid);
        }
        _tab = "edit";
        _selectTab?.Invoke("edit");
        _body.Content = _tabs["edit"];
        await OpenFile(path.Replace('\\', '/'));
        if (line > 0) GoTo(line);
    }

    /// <summary>For --shots: open a tab as if its pill were clicked.</summary>
    public async Task ShotTab(string key)
    {
        _tab = key;
        _selectTab?.Invoke(key);
        _body.Content = _tabs[key];
        if (key == "trace")
        {
            _traceIn.Text = "[14:02:11 ERROR]: Could not pass event PlayerJoinEvent to Shop v1.0\norg.bukkit.event.EventException: null\n" +
                            "Caused by: java.lang.NullPointerException: Cannot invoke \"org.bukkit.entity.Player.getName()\" because \"target\" is null\n" +
                            "\tat com.example.shop.ShopPlugin.onJoin(ShopPlugin.java:21) ~[Shop-1.0.jar:?]\n";
            await Trace(false);
        }
        else await LoadTab();
    }

    Task LoadTab() => _ws.Length == 0 ? Task.CompletedTask : _tab switch
    {
        "skript" => LoadSkript(),
        "java" => LoadJava(),
        _ => Task.CompletedTask,
    };

    // ================================================================ files

    async Task List(string dir)
    {
        if (_ws.Length == 0)
        {
            _tree.Children.Clear();
            _tree.Children.Add(Ui.Empty("No workspace. Create a server, or add a project folder in the AI Assistant.", "folder"));
            return;
        }
        var j = await Ops.Get("/api/dev/files?ws=" + Api.Q(_ws) + "&path=" + Api.Q(dir));
        if (j == null) return;
        _dir = j.S("path", ".");
        _treePath.Text = _dir == "." ? (Workspace()?.S("name") ?? ".") : _dir;
        _tree.Children.Clear();
        foreach (var it in j.A("items").Items())
        {
            var dirItem = it.B("dir");
            var path = it.S("path");
            var b = new Button
            {
                Content = Ui.H(8, Icons.Make(dirItem ? "folder" : "file", 15, dirItem ? Ui.Blue : Ui.Secondary),
                    Ui.T(it.S("name"), null, 13, path == _path ? Ui.Brand : Ui.Text)),
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Left,
            };
            b.Classes.Add("ghost");
            b.Classes.Add("sm");
            b.Click += (_, _) => _ = dirItem ? List(path) : OpenFile(path);
            _tree.Children.Add(b);
        }
        if (_tree.Children.Count == 0) _tree.Children.Add(Ui.Hint("Empty folder."));
    }

    async Task OpenFile(string path)
    {
        if (_path.Length > 0 && _edState.Child is StackPanel sp && sp.Children.OfType<TextBlock>().Any(t => t.Text == "unsaved") &&
            !await Dialogs.Confirm("Unsaved changes", _path + " has changes that are not saved. Open another file anyway?", "Discard"))
            return;
        var j = await Ops.Get("/api/dev/file?ws=" + Api.Q(_ws) + "&path=" + Api.Q(path));
        if (j == null) return;
        _loading = true;
        _path = j.S("path");
        _mtime = j.D("mtime");
        _ed.Text = j.S("content");
        _ed.IsReadOnly = false;
        _ed.ScrollToHome();
        _loading = false;
        _edName.Text = _path;
        Ui.SetPill(_edState, "saved", "on");
        var dir = Parent(_path);
        if (dir != _dir) await List(dir);
        else await List(_dir);
        await Check();
    }

    void GoTo(int line)
    {
        if (line < 1 || line > _ed.Document.LineCount) return;
        var l = _ed.Document.GetLineByNumber(line);
        _ed.TextArea.Caret.Offset = l.Offset;
        _ed.TextArea.Caret.BringCaretToView();
        _ed.Select(l.Offset, l.Length);
        Dispatcher.UIThread.Post(() => _ed.TextArea.Focus(), DispatcherPriority.Background);
    }

    async Task Save()
    {
        if (_path.Length == 0) return;
        try
        {
            var j = await Api.Post("/api/dev/file/save", new JsonObject
            {
                ["ws"] = _ws, ["path"] = _path, ["content"] = _ed.Text, ["mtime"] = _mtime,
            });
            _mtime = j.D("mtime");
            Ui.SetPill(_edState, "saved", "on");
            ShowProblems(j.N("check"));
            Toasts.Good("Saved " + _path + ".");
        }
        catch (ApiException e) when (e.Status == 409)
        {
            if (await Dialogs.Confirm("Changed on disk", _path + " changed since it was opened (perhaps the assistant edited it). Overwrite it with " +
                                                          "what is in the editor?", "Overwrite"))
            {
                _mtime = e.Body.D("disk_mtime");
                await Save();
            }
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
    }

    async Task Check()
    {
        if (_path.Length == 0) return;
        var j = await Ops.Act("/api/dev/check", new JsonObject { ["name"] = _path, ["text"] = _ed.Text }, null);
        ShowProblems(j?.N("check"));
    }

    JsonNode? _lastCheck;

    void ShowProblems(JsonNode? chk)
    {
        _lastCheck = chk;
        _problems.Children.Clear();
        if (chk == null) return;
        var list = chk.A("problems").Items().ToList();
        _problemsHead.Text = "Problems · " + chk.S("summary");
        if (chk.S("note").Length > 0) _problems.Children.Add(Ui.Hint(chk.S("note")));
        if (list.Count == 0)
        {
            _problems.Children.Add(Ui.T(chk.S("format").Length > 0 ? "Nothing to report." : chk.S("summary"), null, 12.5, Ui.Secondary));
            return;
        }
        foreach (var p in list)
        {
            var sev = p.S("severity");
            var line = p.I("line");
            var text = "line " + line + "  " + p.S("message") + (p.S("hint").Length > 0 ? "  - " + p.S("hint") : "");
            var b = new Button
            {
                Content = Ui.Lead(Icons.Make(sev == "error" ? "circle-x" : sev == "warning" ? "alert" : "info", 14,
                        sev == "error" ? Ui.Red : sev == "warning" ? Ui.Orange : Ui.Blue),
                    Ui.T(text, null, 12.5, Ui.Text, wrap: true), 8),
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Left,
            };
            b.Classes.Add("ghost");
            b.Classes.Add("sm");
            b.Click += (_, _) => GoTo(line);
            _problems.Children.Add(b);
        }
    }

    void AskAboutFile()
    {
        if (_path.Length == 0) return;
        var probs = _lastCheck?.A("problems").Items().Take(12)
            .Select(p => "- line " + p.S("line") + ": " + p.S("message")).ToList() ?? new List<string>();
        var text = probs.Count > 0
            ? "Fix the problems in `" + _path + "`:\n" + string.Join("\n", probs)
            : "Review `" + _path + "` and suggest improvements.";
        _ = S.AskAssistant(_ws, text, false);
    }

    // ================================================================ Skript

    async Task LoadSkript()
    {
        _skOut.Children.Clear();
        _skOut.Children.Add(Ui.Empty("reading the scripts…", "puzzle"));
        var j = await Ops.Get("/api/dev/skript?ws=" + Api.Q(_ws), false);
        _skOut.Children.Clear();
        if (j == null) return;
        var env = j.N("environment");
        var sk = env?.N("skript");
        var isServer = Workspace()?.S("kind") == "server";
        if (sk == null && (env?.A("scripts").Count ?? 0) == 0)
        {
            _skOut.Children.Add(Ui.Card(Ui.Empty(isServer ? "Skript is not installed on this server (no Skript jar in plugins/). Add it from Mods & Plugins."
                : "This workspace has no plugins/Skript folder.", "puzzle")));
            return;
        }
        var addons = env!.A("addons").Items().Select(a => a.S("name") + " " + a.S("version")).ToList();
        var envCard = Ui.Card("Skript " + (sk?.S("version") ?? "(not installed)"),
            addons.Count > 0 ? "Addons: " + string.Join(", ", addons) : "No addons installed.",
            Ui.Btn("Re-read", () => _ = LoadSkript(), "refresh", "sm"),
            Ui.Hint("Indentation and structure are checked with Skript's own reader rules. Whether a line is valid Skript is decided by " +
                    "Skript itself - use Reload on a running server to get its verdict."));
        _skOut.Children.Add(envCard);

        var files = j.N("files");
        var list = Ui.V(8);
        foreach (var s in env.A("scripts").Items())
        {
            var name = s.S("name");
            var f = files?.N(name);
            var probs = f?.A("problems").Items().ToList() ?? new List<JsonNode>();
            var errs = probs.Count(p => p.S("severity") == "error");
            var warns = probs.Count(p => p.S("severity") == "warning");
            var info = f?.N("info");
            var sub = string.Join(" · ", new[]
            {
                info == null ? "" : Ui.Plural(info.A("commands").Count, "command"),
                info == null ? "" : Ui.Plural(info.A("events").Count, "event"),
                info == null ? "" : Ui.Plural(info.A("functions").Count, "function"),
                s.B("disabled") ? "disabled (name starts with -)" : "",
            }.Where(x => x.Length > 0));
            var pill = errs > 0 ? Ui.Pill(errs + " error(s)", "bad") : warns > 0 ? Ui.Pill(warns + " warning(s)", "warn") : Ui.Pill("structure ok", "on");
            var rel = "plugins/Skript/scripts/" + name;
            var row = Ui.Row(Ui.TitleSub(name, sub, pill),
                Ui.Btn("Open", () => _ = OpenAt(_ws, rel, 0), "file-text", "sm"),
                isServer ? Ui.Btn("Reload", () => _ = Reload(name), "refresh", "brand sm", "Run 'sk reload' on the server and read Skript's own verdict") : null);
            list.Children.Add(row);
            foreach (var p in probs.Take(6))
            {
                var line = p.I("line");
                var b = Ui.Btn("line " + line + ": " + p.S("message"), () => _ = OpenAt(_ws, rel, line), null, "ghost xs");
                b.Margin = new Thickness(18, 0, 0, 0);
                b.HorizontalAlignment = HorizontalAlignment.Left;
                list.Children.Add(b);
            }
        }
        if (list.Children.Count == 0) list.Children.Add(Ui.Empty("No scripts in plugins/Skript/scripts.", "file"));
        _skOut.Children.Add(Ui.Card("Scripts", null, null, list));

        var cross = j.A("cross").Items().ToList();
        if (cross.Count > 0)
        {
            var col = Ui.V(6);
            foreach (var c in cross.Take(40))
            {
                var file = "plugins/Skript/scripts/" + c.S("file");
                var line = c.I("line");
                var sev = c.S("severity");
                col.Children.Add(Ui.Lead(Icons.Make(sev == "warning" ? "alert" : "info", 14, sev == "warning" ? Ui.Orange : Ui.Blue),
                    Ui.V(2, Ui.T(c.S("message"), null, 12.5, Ui.Text, wrap: true),
                        Ui.Btn(c.S("file") + ":" + line, () => _ = OpenAt(_ws, file, line), "file-text", "ghost xs")
                            .Also(b => b.HorizontalAlignment = HorizontalAlignment.Left)), 8));
            }
            _skOut.Children.Add(Ui.Card("Across the scripts", "Functions called but not defined, commands defined twice, syntax that " +
                                                               "appears to need an addon that is not installed.", null, col));
        }
    }

    async Task Reload(string name)
    {
        var res = Ui.V(6);
        var card = Ui.Card("sk reload " + name, "asking Skript on the server…", null, res);
        _skOut.Children.Insert(1, card);
        try
        {
            var j = await Api.Post("/api/dev/skript/reload", new JsonObject { ["ws"] = _ws, ["script"] = name });
            var rl = j.N("reload");
            var st = rl.S("status");
            res.Children.Add(Ui.Pill(j.S("summary"), st == "ok" ? "on" : st == "errors" ? "bad" : "warn"));
            foreach (var e in rl.A("entries").Items())
            {
                var line = e.I("line");
                res.Children.Add(Ui.Lead(Ui.Btn("line " + line, () => _ = OpenAt(_ws, "plugins/Skript/scripts/" + name, line), "file-text", "ghost xs"),
                    Ui.V(1, Ui.T(e.S("message"), null, 12.5, Ui.Text, wrap: true), Ui.MonoText(e.S("code"), 12, Ui.Secondary)), 6));
            }
            if (st == "no-output") res.Children.Add(Ui.Code(j.S("text")));
        }
        catch (Exception e)
        {
            res.Children.Add(Ui.T(e.Message, null, 13, Ui.Red, wrap: true));
        }
    }

    // ================================================================ Java

    async Task LoadJava()
    {
        _javaOut.Children.Clear();
        _javaOut.Children.Add(Ui.Empty("reading the project…", "package"));
        var j = await Ops.Get("/api/dev/java?ws=" + Api.Q(_ws), false);
        _javaOut.Children.Clear();
        if (j == null) return;
        var p = j.N("project")!;
        if (p.S("kind").Length == 0 && p.A("descriptors").Count == 0)
        {
            _javaOut.Children.Add(Ui.Card(Ui.Empty("No Gradle or Maven build here. Add a plugin or mod project folder as a workspace in the AI Assistant.", "package")));
            return;
        }
        var facts = Ui.V(0,
            Ui.KRow("Build", p.S("kind") + (p.B("wrapper") ? " (wrapper" + (p.S("gradle_version").Length > 0 ? " " + p.S("gradle_version") : "") + ")" : "")),
            Ui.KRow("Platform", p.S("loader", "plain Java")),
            Ui.KRow("Minecraft", p.S("mc", "—")),
            Ui.KRow("Java", p.I("java") > 0 ? p.S("java") : "not set"),
            Ui.KRow("Build command", string.Join(" ", p.Strings("build_command"))));
        var buttons = Ui.H(8,
            Ui.Btn("Build", () => _ = Build("build"), "package", "brand sm"),
            Ui.Btn("Test", () => _ = Build("test"), "circle-check", "sm"),
            Ui.Btn("Ask the assistant to fix the build", () => _ = S.AskAssistant(_ws, "Build the project and fix any compile errors.", false), "sparkles", "sm"));
        _javaOut.Children.Add(Ui.Card(p.S("name", "Project"), null, buttons, facts));

        var deps = Ui.V(4);
        foreach (var d in p.A("dependencies").Items().Take(40))
            deps.Children.Add(Ui.Split(Ui.MonoText(d.S("coord"), 12.5), Ui.Tag(d.S("scope"))));
        if (deps.Children.Count > 0) _javaOut.Children.Add(Ui.Card("Dependencies", null, null, deps));

        var probs = Ui.V(6);
        foreach (var pr in p.A("problems").Items())
            probs.Children.Add(Ui.Lead(Icons.Make(pr.S("severity") == "error" ? "circle-x" : "alert", 14, pr.S("severity") == "error" ? Ui.Red : Ui.Orange),
                Ui.T(pr.S("message") + (pr.S("hint").Length > 0 ? " - " + pr.S("hint") : ""), null, 12.5, Ui.Text, wrap: true), 8));
        foreach (var l in j.A("lint").Items().Take(60))
        {
            var file = l.S("file");
            var line = l.I("line");
            probs.Children.Add(Ui.Lead(Ui.Btn(file.Split('/').Last() + ":" + line, () => _ = OpenAt(_ws, file, line), "file-text", "ghost xs"),
                Ui.T(l.S("message"), null, 12.5, l.S("severity") == "warning" ? Ui.Orange : Ui.Secondary, wrap: true), 6));
        }
        if (probs.Children.Count == 0) probs.Children.Add(Ui.T("Nothing found.", null, 12.5, Ui.Secondary));
        _javaOut.Children.Add(Ui.Card("Things worth a look", "Build-file mismatches, and source patterns that hurt a server. Heuristics - the compiler is the judge.",
            null, probs));
    }

    async Task Build(string task)
    {
        _buildOut.Children.Clear();
        var log = Ui.Selectable("", true, 12, Ui.Text);
        log.TextWrapping = TextWrapping.NoWrap;
        var state = Ui.Pill("starting", "info");
        var errs = Ui.V(4);
        var stop = Ui.Btn("Stop", () => _ = Ops.Act("/api/dev/build/cancel", new JsonObject { ["job"] = _job ?? "" }, null), "stop", "soft-red sm");
        var logBox = new Border
        {
            Background = Ui.Terminal,
            CornerRadius = new CornerRadius(10),
            Padding = new Thickness(12, 8),
            Child = new ScrollViewer { Content = log, MaxHeight = 360, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto },
        };
        _buildOut.Children.Add(Ui.Card(task == "test" ? "Tests" : "Build", null, Ui.H(8, state, stop), Ui.V(10, errs, logBox)));
        JsonNode j;
        try
        {
            j = await Api.Post("/api/dev/build", new JsonObject { ["ws"] = _ws, ["task"] = task });
        }
        catch (Exception e)
        {
            Ui.SetPill(state, e.Message, "bad");
            stop.IsVisible = false;
            return;
        }
        _job = j.Path("job.id") is { } id ? J.Str(id) : null;
        Ui.SetPill(state, "running", "info");
        long since = 0;
        while (_job != null)
        {
            JsonNode jj;
            try { jj = await Api.Get("/api/jobs?id=" + Api.Q(_job) + "&since=" + since); }
            catch (Exception e)
            {
                Ui.SetPill(state, e.Message, "bad");
                break;
            }
            var job = jj.N("job")!;
            since = job.L("next");
            var add = string.Join("\n", job.Strings("log"));
            if (add.Length > 0) log.Text = ((log.Text ?? "") + (log.Text?.Length > 0 ? "\n" : "") + add) is var t && t.Length > 60000 ? t[^60000..] : t;
            if (job.S("state") != "running")
            {
                var ok = job.S("state") == "done";
                var b = job.Path("result.build");
                Ui.SetPill(state, ok ? "succeeded" : b?.S("summary") ?? job.S("error", "failed"), ok ? "on" : "bad");
                stop.IsVisible = false;
                if (b != null)
                {
                    foreach (var e in b.A("errors").Items().Take(30))
                    {
                        var file = e.S("file");
                        var line = e.I("line");
                        errs.Children.Add(Ui.Lead(Ui.Btn(file.Split('/').Last() + ":" + line, () => _ = OpenAt(_ws, file, line), "file-text", "ghost xs"),
                            Ui.T(e.S("message") + (e.S("detail").Length > 0 ? " (" + e.S("detail") + ")" : ""), null, 12.5, Ui.Red, wrap: true), 6));
                    }
                    foreach (var h in b.Strings("hints")) errs.Children.Add(Ui.T("→ " + h, null, 12.5, Ui.Secondary, wrap: true));
                    if (!ok && b.A("errors").Count > 0)
                    {
                        var listed = string.Join("\n", b.A("errors").Items().Take(15)
                            .Select(e => "- " + e.S("file") + ":" + e.S("line") + " " + e.S("message")));
                        var ask = "The build fails with these errors - fix them and build again:\n" + listed;
                        errs.Children.Add(Ui.Btn("Ask the assistant to fix these", () => _ = S.AskAssistant(_ws, ask, false),
                            "sparkles", "sm"));
                    }
                }
                _job = null;
                break;
            }
            await Task.Delay(400);
        }
    }

    // ================================================================ traces

    async Task Trace(bool fromConsole)
    {
        _traceOut.Children.Clear();
        var body = new JsonObject { ["text"] = fromConsole ? "" : _traceIn.Text ?? "", ["ws"] = _ws };
        var j = await Ops.Act("/api/dev/trace", body, null);
        if (j == null) return;
        var traces = j.A("traces").Items().ToList();
        var build = j.N("build");
        if (traces.Count == 0 && (build?.A("errors").Count ?? 0) == 0)
        {
            _traceOut.Children.Add(Ui.Card(Ui.Empty(fromConsole ? "No stack trace in the recent console." : "No stack trace or compile error found in that text.", "search")));
            return;
        }
        foreach (var t in traces.AsEnumerable().Reverse().Take(6))
        {
            var col = Ui.V(10);
            if (t.S("context").Length > 0) col.Children.Add(Ui.T(t.S("context"), null, 12.5, Ui.Secondary, wrap: true));
            var rc = t.N("root_cause");
            col.Children.Add(Ui.V(2, Ui.Eyebrow("Root cause"),
                Ui.T(rc.S("exception"), null, 14, Ui.Red, FontWeight.SemiBold),
                Ui.Selectable(rc.S("message"), false, 13, Ui.Text)));
            var own = t.A("own_frames").Items().ToList();
            if (own.Count == 0)
                col.Children.Add(Ui.Hint("None of its frames are in this workspace's code - pick the project's workspace to link them to its files."));
            if (own.Count > 0)
            {
                var frames = Ui.V(4, Ui.Eyebrow("In this project"));
                foreach (var f in own)
                {
                    var src = f.S("source");
                    var line = f.I("line");
                    var label = f.S("cls").Split('.').Last() + "." + f.S("method") + "(" + f.S("file") + ":" + line + ")";
                    frames.Children.Add(src.Length > 0
                        ? Ui.Btn(label, () => _ = OpenAt(_ws, src, line), "file-text", "ghost xs").Also(b => b.HorizontalAlignment = HorizontalAlignment.Left)
                        : Ui.MonoText(label, 12.5));
                }
                col.Children.Add(frames);
            }
            foreach (var h in t.Strings("hints")) col.Children.Add(Ui.Lead(Icons.Make("info", 14, Ui.Blue), Ui.T(h, null, 13, Ui.Text, wrap: true), 8));
            var chain = string.Join(" → ", new[] { t.S("exception") }.Concat(t.A("caused_by").Items().Select(c => c.S("exception")))
                .Select(x => x.Split('.').Last()));
            var ask = Ui.Btn("Ask the assistant", () => _ = S.AskAssistant(_ws,
                "Explain this error and fix it if the cause is in this project:\n```\n" + t.S("exception") + ": " + t.S("message") +
                "\n" + string.Join("\n", t.A("own_frames").Items().Select(f => "  at " + f.S("cls") + "." + f.S("method") + "(" + f.S("file") + ":" + f.S("line") + ")")) +
                "\n```\nRoot cause: " + rc.S("exception") + ": " + rc.S("message"), false), "sparkles", "sm");
            _traceOut.Children.Add(Ui.Card(chain, null, ask, col));
        }
        if (build != null && build.A("errors").Count > 0)
        {
            var col = Ui.V(4);
            foreach (var e in build.A("errors").Items().Take(30))
            {
                var file = e.S("file");
                var line = e.I("line");
                col.Children.Add(Ui.Lead(Ui.Btn(file.Split('/').Last() + ":" + line, () => _ = OpenAt(_ws, file, line), "file-text", "ghost xs"),
                    Ui.T(e.S("message"), null, 12.5, Ui.Red, wrap: true), 6));
            }
            foreach (var h in build.Strings("hints")) col.Children.Add(Ui.T("→ " + h, null, 12.5, Ui.Secondary, wrap: true));
            _traceOut.Children.Add(Ui.Card("Compile errors", build.S("summary"), null, col));
        }
    }
}
