using System.Text;
using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using Avalonia.VisualTree;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// The development assistant: a local model (Ollama) working inside a
/// workspace - a server's folder or a plugin/mod project - through tools.
/// The chat shows everything it does as it happens: streamed text, each tool
/// call and its result, plans, and an approval card with the diff for every
/// file it wants to change and every command it wants to run.
/// </summary>
public sealed partial class AgentPage : Page
{
    public AgentPage(MainWindow s) : base(s) { }

    public override string Key => "dev";
    public override bool ServerScoped => false;
    public override string Title => "AI Assistant";
    public override string Icon => "sparkles";

    JsonNode _state = new JsonObject();
    JsonNode? _ollama;
    string _ws = "";
    string? _run;
    long _seq;
    CancellationTokenSource? _poll;
    readonly List<JsonObject> _atts = new();

    string _tab = "chat";
    Action<string>? _selectTabBar;
    ContentControl _body = null!;
    Control _chatView = null!;
    StackPanel _settingsView = null!;

    // chat parts
    StackPanel _wsList = null!;
    TextBox _wsSearch = null!;
    StackPanel _msgs = null!;
    ScrollViewer _msgScroll = null!;
    TextBox _input = null!;
    Button _send = null!, _stop = null!;
    WrapPanel _attRow = null!;
    TextBlock _title = null!, _sub = null!, _status = null!, _usage = null!;
    Border _modelPill = null!, _olPill = null!;
    StackPanel _plan = null!, _changes = null!, _memory = null!, _activity = null!;
    Control? _intro;

    // the turn being streamed
    Border? _bubble;
    SelectableTextBlock? _bubbleText;
    readonly StringBuilder _buf = new();
    StackPanel? _thinking;
    SelectableTextBlock? _thinkingText;
    readonly Dictionary<string, (Border row, StackPanel detail, SelectableTextBlock? live)> _tools = new();
    readonly Dictionary<string, Border> _approvals = new();

    protected override Control Build()
    {
        var (bar, selectBar) = Ui.NavTabs(new (string, string, string?)[]
        {
            ("chat", "Chat", "message"), ("skills", "Skills", "puzzle"), ("model", "Model & resources", "cpu"),
        }, _tab, k =>
        {
            _tab = k;
            _body.Content = TabBody(k);
            if (k == "model") _ = LoadSettings();
            if (k == "skills") _ = LoadSkills();
        });
        bar.Margin = new Thickness(0, 0, 0, 14);
        _selectTabBar = selectBar;

        _chatView = BuildChat();
        _settingsView = Ui.V(16);
        _skillsView = Ui.V(16);
        _body = new ContentControl { Content = _chatView };
        var g = new Grid { RowDefinitions = new RowDefinitions("Auto,*"), Margin = new Thickness(24, 16, 24, 20) };
        g.Children.Add(bar);
        Grid.SetRow(_body, 1);
        g.Children.Add(_body);
        return g;
    }

    // ================================================================ chat layout

    Control BuildChat()
    {
        // ---- left: workspaces, memory
        _wsSearch = Ui.Input("", "Find a workspace");
        _wsSearch.Classes.Add("search");
        _wsSearch.TextChanged += (_, _) => PaintWorkspaces();
        _wsList = Ui.V(4);
        _memory = Ui.V(6);
        var left = Ui.V(14,
            Ui.Card(Ui.V(10,
                Ui.Split(Ui.H2("Workspaces"), Ui.IconBtn("folder-plus", "Add a project folder", () => _ = AddWorkspace(), "ghost sm", 30)),
                _wsSearch, _wsList,
                Ui.Hint("Each local server is a workspace. Add a plugin or mod project folder to work on its code.")), 14),
            Ui.Card(Ui.V(10,
                Ui.Split(Ui.H2("Remembered"), Ui.IconBtn("plus", "Add a note the assistant should always know", () => _ = AddNote(), "ghost sm", 30)),
                _memory), 14));

        // ---- middle: the conversation
        _title = Ui.H2("Assistant");
        _sub = Ui.T("", null, 12.5, Ui.Secondary);
        _sub.TextTrimming = TextTrimming.CharacterEllipsis;
        _modelPill = Ui.Pill("no model", "", false);
        _olPill = Ui.Pill("Ollama", "");
        _status = Ui.T("", null, 12.5, Ui.Secondary);
        _usage = Ui.T("", null, 12, Ui.Secondary);
        var titleRow = new WrapPanel();
        foreach (var c in new Control[] { _title, _modelPill, _olPill })
        {
            c.Margin = new Thickness(0, 0, 8, 4);
            c.VerticalAlignment = VerticalAlignment.Center;
            titleRow.Children.Add(c);
        }
        var head = Ui.Split(Ui.V(0, titleRow, _sub).Also(v => v.ClipToBounds = true),
            Ui.H(4, Ui.IconBtn("refresh", "Re-index: bring the project index up to date", () => _ = Reindex(), "ghost sm", 32),
                Ui.IconBtn("trash", "Clear: start a new conversation in this workspace", () => _ = Clear(), "ghost sm", 32)));

        _msgs = Ui.V(14);
        _msgs.Margin = new Thickness(18, 16, 18, 16);
        _msgScroll = new ScrollViewer { Content = _msgs, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
        var msgBox = new Border
        {
            Background = Ui.Terminal,
            CornerRadius = new CornerRadius(12),
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1),
            Margin = new Thickness(14, 0, 14, 0),
            Child = _msgScroll,
        };

        _attRow = new WrapPanel { IsVisible = false };
        _input = Ui.Area("", 2, "Ask about the project, or describe what to build or fix. Enter sends, Shift+Enter adds a line.");
        _input.MaxHeight = 200;
        _input.AddHandler(InputElement.KeyDownEvent, (_, e) =>
        {
            if (e.Key == Avalonia.Input.Key.Enter && !e.KeyModifiers.HasFlag(KeyModifiers.Shift))
            {
                e.Handled = true;
                Send();
            }
        }, Avalonia.Interactivity.RoutingStrategies.Tunnel);
        _send = Ui.Btn("Send", Send, "send", "brand");
        _stop = Ui.Btn("Stop", () => _ = Stop(), "stop", "soft-red");
        _stop.IsVisible = false;
        var attach = Ui.IconBtn("paperclip", "Attach a file from this PC", Attach, "", 38);
        var compose = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto,Auto,Auto") };
        compose.Children.Add(_input);
        attach.Margin = new Thickness(8, 0, 0, 0);
        attach.VerticalAlignment = VerticalAlignment.Bottom;
        Grid.SetColumn(attach, 1);
        compose.Children.Add(attach);
        foreach (var (b, col) in new[] { (_send, 2), (_stop, 3) })
        {
            b.Margin = new Thickness(8, 0, 0, 0);
            b.VerticalAlignment = VerticalAlignment.Bottom;
            Grid.SetColumn(b, col);
            compose.Children.Add(b);
        }
        _slash = Ui.V(2);
        _slash.IsVisible = false;
        _input.TextChanged += (_, _) => PaintSlash();
        var bottom = Ui.V(8, _slash, _attRow, compose, Ui.Split(_status, _usage));
        bottom.Margin = new Thickness(14, 12, 14, 14);
        var midBody = new Grid { RowDefinitions = new RowDefinitions("*,Auto") };
        midBody.Children.Add(msgBox);
        Grid.SetRow(bottom, 1);
        midBody.Children.Add(bottom);
        var middle = Ui.FillCard(head, midBody);

        // ---- right: the task
        _plan = Ui.V(6);
        _changes = Ui.V(6);
        _activity = Ui.V(6);
        var right = Ui.V(14,
            Ui.Card(Ui.V(10, Ui.H2("Plan"), _plan), 14),
            Ui.Card(Ui.V(10, Ui.H2("Files changed"), _changes), 14),
            Ui.Card(Ui.V(10, Ui.H2("Running now"), _activity), 14));

        var grid = new Grid { ColumnDefinitions = new ColumnDefinitions("250,8,*,8,300") };
        var ls = Ui.Scroll(left, new Thickness(0));
        grid.Children.Add(ls);
        var sp1 = new GridSplitter { Background = Brushes.Transparent, ResizeDirection = GridResizeDirection.Columns };
        Grid.SetColumn(sp1, 1);
        grid.Children.Add(sp1);
        Grid.SetColumn(middle, 2);
        grid.Children.Add(middle);
        var sp2 = new GridSplitter { Background = Brushes.Transparent, ResizeDirection = GridResizeDirection.Columns };
        Grid.SetColumn(sp2, 3);
        grid.Children.Add(sp2);
        var rs = Ui.Scroll(right, new Thickness(0));
        Grid.SetColumn(rs, 4);
        grid.Children.Add(rs);

        // Narrow windows: the task column goes first, then the workspace
        // column, so the conversation always keeps room to read.
        var shape = "";
        grid.PropertyChanged += (_, e) =>
        {
            if (e.Property != Visual.BoundsProperty) return;
            var w = grid.Bounds.Width;
            var want = w < 760 ? "chat" : w < 1060 ? "two" : "three";
            if (want == shape) return;
            shape = want;
            grid.ColumnDefinitions[4].Width = new GridLength(want == "three" ? 300 : 0);
            grid.ColumnDefinitions[3].Width = new GridLength(want == "three" ? 8 : 0);
            grid.ColumnDefinitions[0].Width = new GridLength(want == "chat" ? 0 : 250);
            grid.ColumnDefinitions[1].Width = new GridLength(want == "chat" ? 0 : 8);
            rs.IsVisible = sp2.IsVisible = want == "three";
            ls.IsVisible = sp1.IsVisible = want != "chat";
        };
        return grid;
    }

    // ================================================================ loading

    public override async Task OnShow()
    {
        await LoadState();
        await LoadOllama();
        if (_tab == "model") await LoadSettings();
        if (_tab == "skills") await LoadSkills();
        else _ = RefreshSkillNames();
    }

    public override void OnHide()
    {
        // keep polling a running task: its events are still worth showing when the page comes back
    }

    async Task LoadState()
    {
        var j = await Ops.Get("/api/agent/state");
        if (j == null) return;
        _state = j;
        // the sidebar badge is about any task, not only this workspace's
        S.SetBadge("dev", j.A("active").Count > 0 || _run != null ? "working" : null, Ui.Brand);
        if (!j.B("available"))
        {
            _msgs.Children.Clear();
            _msgs.Children.Add(Ui.Empty(j.S("error", "The assistant could not be loaded."), "alert"));
            return;
        }
        var wss = j.A("workspaces").Items().ToList();
        var want = _ws;
        if (want.Length == 0) want = S.Prefs.DevWorkspace;
        var cur = S.State.Path("server.id") is { } sid ? "srv-" + J.Str(sid) : "";
        if (want.Length == 0 || wss.All(w => w.S("id") != want))
            want = wss.Any(w => w.S("id") == cur) ? cur : wss.FirstOrDefault()?.S("id") ?? "";
        PaintWorkspaces();
        if (want != _ws || _msgs.Children.Count == 0) await Open(want);
        else PaintHeader();
    }

    /// <summary>The workspace list again, without switching away from this one.</summary>
    async Task RefreshWorkspaces()
    {
        var j = await Ops.Get("/api/agent/state", false);
        if (j == null) return;
        _state = j;
        PaintWorkspaces();
    }

    async Task LoadOllama(bool fresh = false)
    {
        var j = await Ops.Get("/api/agent/ollama" + (fresh ? "?fresh=1" : ""), false);
        _ollama = j;
        if (j == null) return;
        if (j.B("running"))
            Ui.SetPill(_olPill, "Ollama " + j.S("version"), "on");
        else
            Ui.SetPill(_olPill, "Ollama not running", "bad");
        ToolTip.SetTip(_olPill, j.B("running") ? j.S("base") : j.S("error"));
        PaintHeader();
        if (_msgs.Children.Count == 1 && _intro != null) ShowIntro();
    }

    JsonNode? Workspace() => _state.A("workspaces").Items().FirstOrDefault(w => w.S("id") == _ws);

    void PaintWorkspaces()
    {
        _wsList.Children.Clear();
        var q = (_wsSearch.Text ?? "").Trim().ToLowerInvariant();
        var items = _state.A("workspaces").Items()
            .Where(w => q.Length == 0 || w.S("name").ToLowerInvariant().Contains(q) || w.S("root").ToLowerInvariant().Contains(q))
            .ToList();
        if (items.Count == 0)
        {
            _wsList.Children.Add(Ui.Hint(q.Length > 0 ? "Nothing matches." : "No workspaces yet - create a server or add a folder."));
            return;
        }
        foreach (var w in items)
        {
            var id = w.S("id");
            var icon = w.S("kind") == "server" ? "server" : w.S("kind") == "computer" ? "drive" : "code";
            var sub = w.S("kind") == "server"
                ? (w.S("software") + " " + w.S("mc")).Trim()
                : w.S("root");
            var body = Ui.V(1, Ui.T(w.S("name"), null, 13.5, Ui.Contrast, FontWeight.SemiBold),
                Ui.T(sub.Length > 0 ? sub : "server", null, 11.5, Ui.Secondary).Also(t => t.TextTrimming = TextTrimming.CharacterEllipsis));
            var b = new Button { Content = Ui.Lead(Icons.Make(icon, 17, w.B("exists") ? Ui.Text : Ui.Red), body, 10) };
            b.Classes.Add("nav");
            b.Classes.Set("active", id == _ws);
            b.HorizontalAlignment = HorizontalAlignment.Stretch;
            ToolTip.SetTip(b, w.S("root") + (w.B("exists") ? "" : "\n(the folder is missing)"));
            b.Click += (_, _) => _ = Open(id);
            if (w.S("kind") == "project")
                b.ContextMenu = new ContextMenu
                {
                    ItemsSource = new[]
                    {
                        new MenuItem { Header = "Remove from the list" }.Also(m => m.Click += async (_, _) =>
                        {
                            if (!await Dialogs.Confirm("Remove workspace", "Remove " + w.S("name") +
                                " from the assistant? The folder itself is not touched.", "Remove")) return;
                            await Ops.Act("/api/agent/workspace/remove", new JsonObject { ["id"] = id }, "Removed.");
                            if (_ws == id) _ws = "";
                            await LoadState();
                        }),
                    },
                };
            _wsList.Children.Add(b);
        }
    }

    void PaintHeader()
    {
        var w = Workspace();
        _title.Text = w?.S("name") ?? "Assistant";
        _sub.Text = w == null ? "" : (w.S("kind") == "server" ? "Server folder · " : w.S("kind") == "computer"
            ? "Whole computer · " : "Project · ") + w.S("root");
        var model = _state.Path("settings.model") is { } m ? J.Str(m) : "";
        if (model.Length == 0)
        {
            Ui.SetPill(_modelPill, "choose a model", "warn", false);
            ToolTip.SetTip(_modelPill, "Pick one under Model & resources");
            return;
        }
        var info = _ollama?.A("models").Items().FirstOrDefault(x => x.S("name") == model);
        var mode = info?.S("mode") ?? "";
        Ui.SetPill(_modelPill, model + (mode == "json" ? " · JSON mode" : mode == "native" ? " · tools" : ""),
            mode == "json" ? "warn" : "info", false);
        ToolTip.SetTip(_modelPill, info == null ? "Not installed in Ollama" :
            mode == "json" ? "This model cannot call tools natively, so the assistant uses a JSON reply format instead (" + info.S("mode_why") + ")."
                           : "Native tool calling (" + info.S("mode_why") + ").");
    }

    async Task Open(string wsid)
    {
        _poll?.Cancel();
        _ws = wsid;
        S.Prefs.DevWorkspace = wsid;
        S.Prefs.Save();
        PaintWorkspaces();
        PaintHeader();
        _msgs.Children.Clear();
        _plan.Children.Clear();
        _plan.Children.Add(Ui.Hint("The assistant shows its plan here for multi-step work."));
        _changes.Children.Clear();
        _activity.Children.Clear();
        _activity.Children.Add(Ui.Hint("Nothing running."));
        _run = null;
        SetBusy(false);
        if (wsid.Length == 0)
        {
            ShowIntro();
            return;
        }
        await LoadHistory();
        await LoadMemory();
        await LoadChanges();
        var active = _state.A("active").Items().FirstOrDefault(r => r.S("ws") == wsid);
        if (active != null) Attach(active.S("id"));
    }

    void ShowIntro()
    {
        _msgs.Children.Clear();
        var running = _ollama?.B("running") == true;
        var hasModel = (_state.Path("settings.model") is { } m) && J.Str(m).Length > 0;
        Control body;
        if (!running)
        {
            body = Ui.V(10,
                Ui.T("Ollama is not running", "h3"),
                Ui.T("The assistant runs on your own computer with Ollama - free, no account, nothing leaves the machine.",
                    null, 13.5, Ui.Secondary, wrap: true),
                Markdown.Render("1. Install Ollama from `https://ollama.com/download` (free).\n" +
                                "2. Start it - the app does that, or run `ollama serve`.\n" +
                                "3. Pull a model that can call tools, for example `ollama pull qwen2.5-coder:7b`. " +
                                "Ollama's library marks them: https://ollama.com/search?c=tool\n" +
                                "4. Pick it under **Model & resources**.", null, S.Copy),
                Ui.H(8, Ui.Btn("Check again", () => _ = LoadOllama(true), "refresh", "brand sm"),
                    Ui.Btn("Model & resources", () => OpenTab("model"), "cpu", "sm")));
        }
        else if (!hasModel)
        {
            body = Ui.V(10, Ui.T("Pick a model", "h3"),
                Ui.T("Ollama is running. Choose which installed model the assistant uses - models that support tool calling work best.",
                    null, 13.5, Ui.Secondary, wrap: true),
                Ui.Btn("Model & resources", () => OpenTab("model"), "cpu", "brand sm"));
        }
        else
        {
            body = Ui.V(10, Ui.T("What should we work on?", "h3"),
                Ui.T("The assistant reads the whole project, edits files, runs the build and checks its own work. Every file change and " +
                     "every command is shown to you first.", null, 13.5, Ui.Secondary, wrap: true),
                Suggestions());
        }
        _intro = new Border
        {
            Background = Ui.Raised,
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(14),
            Padding = new Thickness(20, 18),
            MaxWidth = 720,
            HorizontalAlignment = HorizontalAlignment.Center,
            Child = body,
        };
        _msgs.Children.Add(_intro);
    }

    Control Suggestions()
    {
        var w = Workspace();
        var server = w?.S("kind") == "server";
        var ideas = w?.S("kind") == "computer"
            ? new[] { "Start my server, wait until it is up and tell me if anything went wrong.", "Find my plugin projects and build the newest one.",
                      "Search the web for the latest Paper version and tell me what changed.", "Look at my Downloads folder and tell me which server jars are in it." }
            : server
            ? new[] { "Why did the server crash last? Check the logs.", "Check every Skript script and fix what Skript rejects.",
                      "Review server.properties and the plugin configs for mistakes.", "Write a Skript /spawn command with a 3 second warmup." }
            : new[] { "Give me an overview of this project.", "Build the project and fix any compile errors.",
                      "Add a /hello command to the plugin, with a permission.", "Look for code that blocks the server thread." };
        var wrap = new WrapPanel();
        foreach (var idea in ideas)
        {
            var text = idea;
            var b = Ui.Btn(text, () =>
            {
                _input.Text = text;
                _input.Focus();
            }, null, "sm");
            b.Margin = new Thickness(0, 0, 8, 8);
            wrap.Children.Add(b);
        }
        return wrap;
    }

    /// <summary>For --shots.</summary>
    public async Task ShotTab(string key)
    {
        OpenTab(key);
        if (key == "model") await LoadSettings();
        if (key == "skills") await LoadSkills();
    }

    void OpenTab(string key)
    {
        _selectTabBar?.Invoke(key);
        _tab = key;
        _body.Content = TabBody(key);
        if (key == "model") _ = LoadSettings();
        if (key == "skills") _ = LoadSkills();
    }

    async Task LoadHistory()
    {
        var j = await Ops.Get("/api/agent/history?ws=" + Api.Q(_ws), false);
        _msgs.Children.Clear();
        var items = j?.A("items").Items().ToList() ?? new List<JsonNode>();
        if (items.Count == 0)
        {
            ShowIntro();
            return;
        }
        _intro = null;
        foreach (var h in items.TakeLast(40))
        {
            if (h.S("role") == "user")
                UserBubble(h.S("content"));
            else
            {
                var tools = h.A("tools").Items().ToList();
                if (tools.Count > 0)
                {
                    var names = string.Join(", ", tools.Select(t => t.S("name")).Distinct().Take(8));
                    _msgs.Children.Add(Ui.T("used " + Ui.Plural(tools.Count, "tool") + ": " + names, null, 12, Ui.Secondary)
                        .Also(t => t.Margin = new Thickness(4, 0)));
                }
                var st = h.S("state");
                if (h.S("content").Length > 0)
                    AssistantBubble(h.S("content"), st == "question" ? "question" : st == "error" || st == "cancelled" ? "muted" : "");
                else if (st == "error" || st == "cancelled")
                    _msgs.Children.Add(Ui.T(st == "cancelled" ? "stopped" : "ended with an error", null, 12, Ui.Secondary));
            }
        }
        ScrollEnd();
    }

    async Task LoadMemory()
    {
        var j = await Ops.Get("/api/agent/memory?ws=" + Api.Q(_ws), false);
        _memory.Children.Clear();
        var items = j?.A("items").Items().ToList() ?? new List<JsonNode>();
        if (items.Count == 0)
        {
            _memory.Children.Add(Ui.Hint("Facts the assistant saves about this project appear here, and are given to it in every conversation."));
            return;
        }
        foreach (var n in items.AsEnumerable().Reverse())
        {
            var id = n.S("id");
            var row = Ui.Split(Ui.T(n.S("text"), null, 12.5, Ui.Text, wrap: true),
                Ui.IconBtn("x", "Forget this", () => _ = Forget(id), "ghost xs", 24));
            _memory.Children.Add(row);
        }
    }

    async Task LoadChanges()
    {
        var path = "/api/agent/changes?ws=" + Api.Q(_ws) + (_run != null ? "&run=" + Api.Q(_run) : "");
        var j = await Ops.Get(path, false);
        _changes.Children.Clear();
        var items = j?.A("items").Items().Take(12).ToList() ?? new List<JsonNode>();
        if (items.Count == 0)
        {
            _changes.Children.Add(Ui.Hint("Every file the assistant changes is listed here, and can be put back."));
            return;
        }
        foreach (var c in items) _changes.Children.Add(ChangeRow(c));
    }

    Control ChangeRow(JsonNode c)
    {
        var id = c.S("id");
        var reverted = c.B("reverted");
        var label = Ui.V(1,
            Ui.T(c.S("path"), null, 12.5, reverted ? Ui.Secondary : Ui.Contrast).Also(t => t.TextTrimming = TextTrimming.CharacterEllipsis),
            Ui.T(reverted ? "put back" : c.B("created") ? "created" : c.S("by") == "editor" ? "saved in the editor"
                : "+" + c.S("added", "0") + " -" + c.S("removed", "0"), null, 11.5, Ui.Secondary));
        var open = Ui.IconBtn("file-text", "Open in the code editor", () => S.OpenCode(_ws, c.S("path"), 0), "ghost xs", 24);
        var undo = reverted ? null : Ui.IconBtn("restart", "Put the file back as it was", () => _ = Revert(id, c.S("path")), "ghost xs", 24);
        return Ui.Split(label, Ui.H(2, open, undo));
    }

    // ================================================================ sending

    async void Send()
    {
        var text = (_input.Text ?? "").Trim();
        if (text.Length == 0 || _run != null) return;
        if (_ws.Length == 0)
        {
            Toasts.Bad("Pick a workspace first.");
            return;
        }
        var atts = new JsonArray();
        foreach (var a in _atts) atts.Add(a.DeepClone());
        try
        {
            var j = await Api.Post("/api/agent/run", new JsonObject { ["ws"] = _ws, ["message"] = text, ["attachments"] = atts });
            _input.Text = "";
            _atts.Clear();
            RenderAtts();
            if (_intro != null)
            {
                _msgs.Children.Remove(_intro);
                _intro = null;
            }
            UserBubble(text);
            Attach(j.Path("run.id") is { } id ? J.Str(id) : "");
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
    }

    /// <summary>From other pages: open a workspace and send (or prefill) a request.</summary>
    public async Task Ask(string wsid, string text, bool send)
    {
        if (_state.A("workspaces").Count == 0) await LoadState();
        if (wsid.Length > 0 && wsid != _ws) await Open(wsid);
        OpenTab("chat");
        _input.Text = text;
        if (send) Send();
        else _input.Focus();
    }

    void Attach(string runId)
    {
        if (runId.Length == 0) return;
        _poll?.Cancel();
        _run = runId;
        _seq = 0;
        _bubble = null;
        _buf.Clear();
        _thinking = null;
        _tools.Clear();
        _approvals.Clear();
        _plan.Children.Clear();
        _plan.Children.Add(Ui.Hint("No plan yet."));
        SetBusy(true);
        var cts = _poll = new CancellationTokenSource();
        _ = Poll(runId, cts.Token);
    }

    async Task Poll(string runId, CancellationToken ct)
    {
        var errors = 0;
        while (!ct.IsCancellationRequested)
        {
            JsonNode j;
            try
            {
                j = await Api.Get("/api/agent/events?run=" + Api.Q(runId) + "&since=" + _seq + "&wait=10");
                errors = 0;
            }
            catch (Exception e)
            {
                if (ct.IsCancellationRequested) return;
                if (++errors > 5)
                {
                    ErrorBubble("Lost touch with the task: " + e.Message);
                    Finish();
                    return;
                }
                await Task.Delay(1500, CancellationToken.None);
                continue;
            }
            if (ct.IsCancellationRequested || _run != runId) return;
            foreach (var ev in j.A("events").Items())
            {
                _seq = ev.L("seq");
                Handle(ev);
                if (ev.S("type") == "done")
                {
                    Finish();
                    return;
                }
            }
        }
    }

    void Handle(JsonNode ev)
    {
        switch (ev.S("type"))
        {
            case "status":
                var st = ev.S("state");
                _status.Text = st switch
                {
                    "queued" => "waiting for another task to finish" + (ev.S("detail").Length > 0 ? "" : ""),
                    "thinking" => "thinking… (" + ev.S("detail") + ")",
                    "tool" => "working: " + ev.S("detail"),
                    "waiting" => "waiting for you: " + ev.S("detail"),
                    "running" => ev.S("detail").Length > 0 ? ev.S("detail") : "working…",
                    _ => st,
                };
                _status.Foreground = st == "waiting" ? Ui.Orange : Ui.Secondary;
                PaintActivity(st, ev.S("detail"));
                break;
            case "model":
                if (ev.S("mode") == "json")
                    Note("This model has no native tool calling, so the assistant uses a JSON reply format (" + ev.S("why") + ").");
                break;
            case "thinking":
                Thinking(ev.S("text"));
                break;
            case "text":
                Stream(ev.S("text"));
                break;
            case "tool":
                CloseBubble();
                ToolRow(ev);
                break;
            case "tool_result":
                ToolDone(ev);
                break;
            case "output":
                ToolOutput(ev.S("call_id"), ev.S("text"));
                break;
            case "approval":
                CloseBubble();
                ApprovalCard(ev);
                break;
            case "approval_done":
                ApprovalDone(ev);
                break;
            case "plan":
                PaintPlan(ev);
                break;
            case "change":
                _ = LoadChanges();
                break;
            case "memory":
                _ = LoadMemory();
                Note("Remembered: " + ev.S("text"));
                break;
            case "skill":
                SkillNote(ev);
                break;
            case "workspace_added":
                Note("New workspace: " + ev.S("name") + " (" + ev.S("root") + ") - it is in the list on the left.");
                _ = RefreshWorkspaces();
                break;
            case "skill_saved":
                Note("Saved the skill " + ev.S("name") + " - it is used when a request matches it, or start it with /" + ev.S("name") + ".");
                _ = RefreshSkillNames();
                break;
            case "message":
                FinalMessage(ev.S("text"));
                break;
            case "question":
                break;
            case "usage":
                _usage.Text = "step " + ev.S("step") + " · " + ev.S("in") + " in / " + ev.S("out") + " out tokens" +
                              (ev.L("load_ms") > 1000 ? " · model load " + (ev.L("load_ms") / 1000.0).ToString("0.0") + " s" : "");
                break;
            case "error":
                CloseBubble();
                if (ev.B("cancelled")) Note("Stopped.");
                else ErrorBubble(ev.S("text"));
                break;
        }
    }

    void Finish()
    {
        CloseBubble();
        _run = null;
        SetBusy(false);
        _status.Text = "";
        _activity.Children.Clear();
        _activity.Children.Add(Ui.Hint("Nothing running."));
        _ = LoadChanges();
        _ = LoadState();
    }

    void SetBusy(bool busy)
    {
        _send.IsVisible = !busy;
        _stop.IsVisible = busy;
        if (busy) S.SetBadge("dev", "working", Ui.Brand);
    }

    async Task Stop()
    {
        if (_run == null) return;
        await Ops.Act("/api/agent/cancel", new JsonObject { ["run"] = _run }, null);
    }

    // ================================================================ rendering a turn

    void UserBubble(string text)
    {
        var body = Ui.Selectable(text, false, 14, Ui.Contrast);
        _msgs.Children.Add(new Border
        {
            Background = Ui.BrandHighlight,
            BorderBrush = Ui.Hex("#661BD96A"),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(16, 16, 4, 16),
            Padding = new Thickness(14, 10),
            MaxWidth = 680,
            HorizontalAlignment = HorizontalAlignment.Right,
            Child = body,
        });
        ScrollEnd();
    }

    Border AssistantBubble(string markdown, string kind = "")
    {
        var b = new Border
        {
            Background = kind == "question" ? Ui.BlueHighlight : Ui.Raised,
            BorderBrush = kind == "question" ? Ui.Hex("#664F9CFF") : Ui.Divider,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(16, 16, 16, 4),
            Padding = new Thickness(16, 12),
            MaxWidth = 820,
            HorizontalAlignment = HorizontalAlignment.Left,
            Opacity = kind == "muted" ? 0.75 : 1,
            Child = Markdown.Render(markdown, OpenFile, S.Copy),
        };
        _msgs.Children.Add(b);
        ScrollEnd();
        return b;
    }

    void OpenFile(string path, int line) => S.OpenCode(_ws, path, line);

    void Stream(string text)
    {
        if (_bubble == null)
        {
            _buf.Clear();
            _bubbleText = Ui.Selectable("", false, 14, Ui.Text);
            _bubbleText.LineHeight = 21;
            _bubble = new Border
            {
                Background = Ui.Raised,
                BorderBrush = Ui.Divider,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(16, 16, 16, 4),
                Padding = new Thickness(16, 12),
                MaxWidth = 820,
                HorizontalAlignment = HorizontalAlignment.Left,
                Child = _bubbleText,
            };
            _msgs.Children.Add(_bubble);
        }
        _buf.Append(text);
        _bubbleText!.Text = _buf.ToString();
        ScrollEnd();
    }

    /// <summary>A tool call ends a stretch of streamed text: render what came as Markdown.</summary>
    void CloseBubble()
    {
        if (_bubble != null && _buf.Length > 0)
            _bubble.Child = Markdown.Render(_buf.ToString(), OpenFile, S.Copy);
        else if (_bubble != null)
            _msgs.Children.Remove(_bubble);
        _bubble = null;
        _bubbleText = null;
        _buf.Clear();
        _thinking = null;
    }

    void FinalMessage(string text)
    {
        if (_bubble != null)
        {
            _bubble.Child = Markdown.Render(text, OpenFile, S.Copy);
            _bubble = null;
            _bubbleText = null;
            _buf.Clear();
        }
        else
            AssistantBubble(text);
        ScrollEnd();
    }

    void Thinking(string text)
    {
        if (_thinking == null)
        {
            _thinkingText = Ui.Selectable("", false, 12.5, Ui.Secondary);
            var holder = new Border
            {
                Child = _thinkingText,
                IsVisible = false,
                BorderBrush = Ui.Ring,
                BorderThickness = new Thickness(2, 0, 0, 0),
                Padding = new Thickness(10, 2),
                MaxHeight = 220,
            };
            var toggle = Ui.Btn("thinking…", null, "sparkles", "ghost xs");
            toggle.Click += (_, _) => holder.IsVisible = !holder.IsVisible;
            _thinking = Ui.V(4, toggle, holder);
            _msgs.Children.Add(_thinking);
        }
        _thinkingText!.Text += text;
    }

    void Note(string text)
    {
        _msgs.Children.Add(Ui.T(text, null, 12, Ui.Secondary, wrap: true).Also(t => t.Margin = new Thickness(4, 0)));
        ScrollEnd();
    }

    void ErrorBubble(string text)
    {
        _msgs.Children.Add(new Border
        {
            Background = Ui.RedHighlight,
            BorderBrush = Ui.Hex("#66FF496E"),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(12),
            Padding = new Thickness(14, 10),
            MaxWidth = 820,
            HorizontalAlignment = HorizontalAlignment.Left,
            Child = Ui.Lead(Icons.Make("alert", 18, Ui.Red), Ui.Selectable(text, false, 13.5, Ui.Red)),
        });
        ScrollEnd();
    }

    static readonly Dictionary<string, string> ToolIcons = new()
    {
        ["project_overview"] = "layers", ["list_files"] = "folder", ["read_file"] = "file-text", ["find_files"] = "search",
        ["search_code"] = "search", ["check_file"] = "circle-check", ["explain_error"] = "alert", ["write_file"] = "file-plus",
        ["edit_file"] = "edit", ["run_build"] = "package", ["run_command"] = "console", ["remember"] = "save",
        ["ask_user"] = "message", ["set_plan"] = "list", ["server_status"] = "server", ["read_console"] = "console",
        ["server_command"] = "zap", ["skript_environment"] = "puzzle", ["skript_reload"] = "refresh",
        ["list_servers"] = "server", ["start_server"] = "play", ["stop_server"] = "stop", ["restart_server"] = "restart",
        ["run_shell"] = "console", ["web_search"] = "search", ["fetch_url"] = "globe",
        ["use_skill"] = "puzzle", ["read_skill_file"] = "puzzle", ["save_skill"] = "puzzle",
        ["server_software"] = "server", ["create_server"] = "plus", ["find_plugins"] = "search", ["install_plugins"] = "package",
        ["create_project"] = "folder-plus", ["install_tool"] = "download",
    };

    static string ArgLine(JsonNode? args)
    {
        if (args is not JsonObject o || o.Count == 0) return "";
        var parts = new List<string>();
        foreach (var (k, v) in o)
        {
            var s = J.Str(v);
            if (s.Length > 70) s = s[..70] + "…";
            parts.Add(k + "=" + s.Replace("\n", " "));
        }
        return string.Join("  ", parts);
    }

    void ToolRow(JsonNode ev)
    {
        var id = ev.S("call_id");
        var name = ev.S("name");
        var icon = Icons.Make(ToolIcons.GetValueOrDefault(name, "wrench"), 15, Ui.Secondary);
        var title = Ui.T(name.Replace('_', ' '), null, 13, Ui.Contrast, FontWeight.SemiBold);
        var args = Ui.T(ArgLine(ev.N("args")), null, 12, Ui.Secondary).Also(t => t.TextTrimming = TextTrimming.CharacterEllipsis);
        var spinner = Icons.Make("refresh", 14, Ui.Brand);
        spinner.Classes.Add("spin");
        spinner.RenderTransform = new RotateTransform();
        var detail = Ui.V(6);
        detail.IsVisible = false;
        var head = Ui.Split(Ui.Lead(icon, Ui.V(1, title, args), 10), spinner);
        var row = new Border
        {
            Background = Ui.Hex("#14FFFFFF"),
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(10),
            Padding = new Thickness(12, 8),
            MaxWidth = 820,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            Child = Ui.V(6, head, detail),
            Cursor = new Cursor(StandardCursorType.Hand),
        };
        row.PointerPressed += (_, e) =>
        {
            if (e.Source is Visual v && v.FindAncestorOfType<SelectableTextBlock>(true) != null) return;
            detail.IsVisible = !detail.IsVisible;
        };
        _tools[id] = (row, detail, null);
        _msgs.Children.Add(row);
        ScrollEnd();
    }

    void ToolOutput(string id, string text)
    {
        if (!_tools.TryGetValue(id, out var t)) return;
        if (t.live == null)
        {
            var live = Ui.Selectable("", true, 12, Ui.Text);
            live.TextWrapping = TextWrapping.NoWrap;
            t.detail.Children.Add(new Border
            {
                Background = Ui.Terminal,
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(10, 6),
                Child = new ScrollViewer { Content = live, MaxHeight = 220, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto },
            });
            t.detail.IsVisible = true;
            _tools[id] = (t.row, t.detail, live);
            t = _tools[id];
        }
        var cur = t.live!.Text ?? "";
        var next = cur + (cur.Length > 0 ? "\n" : "") + text;
        if (next.Length > 30000) next = next[^30000..];
        t.live.Text = next;
        ScrollEnd();
    }

    void ToolDone(JsonNode ev)
    {
        if (!_tools.TryGetValue(ev.S("call_id"), out var t)) return;
        var ok = ev.B("ok");
        var head = (Grid)((StackPanel)t.row.Child!).Children[0];
        var badge = Ui.H(6, Ui.T(ev.S("summary"), null, 12, ok ? Ui.Secondary : Ui.Red).Also(x =>
            {
                x.MaxWidth = 360;
                x.TextTrimming = TextTrimming.CharacterEllipsis;
            }),
            Icons.Make(ok ? "circle-check" : "circle-x", 15, ok ? Ui.Brand : Ui.Red));
        ToolTip.SetTip(badge, ev.S("summary") + " · " + ev.S("ms") + " ms");
        head.Children.RemoveAt(head.Children.Count - 1);
        badge.Margin = new Thickness(12, 0, 0, 0);
        Grid.SetColumn(badge, 1);
        head.Children.Add(badge);
        // structured results first, raw output under them
        var data = ev.N("data");
        if (data?.N("check") is { } chk) t.detail.Children.Add(Problems(chk, ev.S("summary")));
        if (data?.N("build") is { } bld && bld.A("errors").Count > 0) t.detail.Children.Add(BuildErrors(bld));
        if (data?.N("reload") is { } rl) t.detail.Children.Add(ReloadResult(rl));
        if (t.live == null && ev.S("output").Length > 0)
        {
            var o = Ui.Selectable(ev.S("output"), true, 12, Ui.Text);
            o.TextWrapping = TextWrapping.NoWrap;
            t.detail.Children.Add(new Border
            {
                Background = Ui.Terminal,
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(10, 6),
                Child = new ScrollViewer { Content = o, MaxHeight = 240, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto },
            });
        }
        if (!ok) t.detail.IsVisible = true;
    }

    Control Problems(JsonNode chk, string title)
    {
        var col = Ui.V(4);
        foreach (var p in chk.A("problems").Items().Take(30))
        {
            var sev = p.S("severity");
            col.Children.Add(Ui.Lead(Icons.Make(sev == "error" ? "circle-x" : sev == "warning" ? "alert" : "info", 13,
                    sev == "error" ? Ui.Red : sev == "warning" ? Ui.Orange : Ui.Blue),
                Ui.T("line " + p.S("line") + ": " + p.S("message") + (p.S("hint").Length > 0 ? " - " + p.S("hint") : ""),
                    null, 12.5, Ui.Text, wrap: true), 8));
        }
        if (col.Children.Count == 0) col.Children.Add(Ui.T(chk.S("summary", title), null, 12.5, Ui.Secondary));
        return col;
    }

    Control BuildErrors(JsonNode bld)
    {
        var col = Ui.V(4);
        foreach (var e in bld.A("errors").Items().Take(20))
        {
            var file = e.S("file");
            var line = e.I("line");
            var link = Ui.Btn(file.Split('/').Last() + ":" + line, () => OpenFile(file, line), "file-text", "ghost xs");
            col.Children.Add(Ui.Lead(link, Ui.T(e.S("message") + (e.S("detail").Length > 0 ? " (" + e.S("detail") + ")" : ""),
                null, 12.5, Ui.Red, wrap: true), 6));
        }
        foreach (var h in bld.Strings("hints")) col.Children.Add(Ui.T("→ " + h, null, 12.5, Ui.Secondary, wrap: true));
        return col;
    }

    Control ReloadResult(JsonNode rl)
    {
        var col = Ui.V(4);
        var st = rl.S("status");
        col.Children.Add(Ui.Pill(st == "ok" ? "Skript: reloaded cleanly" : st == "errors" ? "Skript: " + rl.S("error_count") + " error(s)"
            : st == "warnings" ? "Skript: warnings" : "Skript said nothing", st == "ok" ? "on" : st == "errors" ? "bad" : "warn"));
        foreach (var e in rl.A("entries").Items())
            col.Children.Add(Ui.T("line " + e.S("line") + ": " + e.S("message") + (e.S("code").Length > 0 ? "\n    " + e.S("code") : ""),
                null, 12.5, Ui.Text, wrap: true));
        return col;
    }

    void ApprovalCard(JsonNode ev)
    {
        var id = ev.S("call_id");
        var kind = ev.S("kind");
        var content = Ui.V(10);
        var icon = kind switch { "write" => "edit", "command" or "shell" => "console", "web" => "globe", "install" => "download", _ => "zap" };
        var where = kind switch
        {
            "write" => ev.B("created") ? "new file" : "+" + ev.S("added") + " -" + ev.S("removed"),
            "command" => "in " + ev.S("cwd"),
            "shell" => ev.S("shell") + " in " + ev.S("cwd"),
            "web" => "reads this page from the internet",
            "install" => "downloads from the official source, checked against its published checksum",
            _ => ev.S("server").Length > 0 ? "on " + ev.S("server") : "on the server",
        };
        content.Children.Add(Ui.Split(Ui.Lead(Icons.Make(icon, 18, Ui.Orange), Ui.V(1,
                Ui.T(ev.S("title"), null, 14, Ui.Contrast, FontWeight.SemiBold),
                Ui.T(where, null, 12, Ui.Secondary))),
            Ui.Pill("needs your approval", "warn")));
        if (ev.S("url").Length > 0) content.Children.Add(Ui.Code(ev.S("url")));
        if (ev.S("eula").Length > 0)
        {
            var eula = ev.S("eula");
            content.Children.Add(Ui.V(4,
                Ui.T("Approving also accepts the Minecraft End User License Agreement for this server, as you. Read it first:", null, 12.5, Ui.Orange, wrap: true),
                Ui.Btn(eula, () => MainWindow.OpenUrl(eula), "external", "ghost xs")));
        }
        if (ev.S("diff").Length > 0) content.Children.Add(Markdown.Diff(ev.S("diff")));
        if (ev.S("command").Length > 0) content.Children.Add(Ui.Code(ev.S("command")));
        var checks = ev.N("checks");
        if (checks != null && checks.A("problems").Count > 0)
            content.Children.Add(Ui.V(6, Ui.T("Checking the new content: " + checks.S("summary"), null, 12.5,
                checks.B("ok") ? Ui.Secondary : Ui.Red, FontWeight.SemiBold), Problems(checks, "")));
        else if (checks != null && checks.S("format").Length > 0)
            content.Children.Add(Ui.T("Checked the new content: " + checks.S("summary"), null, 12.5, Ui.Secondary));
        var always = ev.S("always", "Allow all for this task");
        var buttons = Ui.H(8,
            Ui.Btn(kind switch { "write" => "Apply change", "web" => "Open it", "server" => "Do it", "install" => "Install", _ => "Run it" },
                () => _ = Decide(id, "approve", ""), "check", "brand sm"),
            Ui.Btn("Reject…", () => _ = RejectWithNote(id), "x", "soft-red sm"),
            Ui.Btn(always, () => _ = Decide(id, "always", ""), null, "ghost sm",
                kind is "command" or "shell" ? "Run this exact command from now on without asking" : "Approve the rest of this task's " +
                (kind switch { "write" => "file changes", "web" => "page reads", "install" => "installs", _ => "server actions" }) + " without asking"));
        content.Children.Add(buttons);
        var card = new Border
        {
            Background = Ui.Raised,
            BorderBrush = Ui.Orange,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(12),
            Padding = new Thickness(16, 12),
            MaxWidth = 860,
            Child = content,
        };
        _approvals[id] = card;
        _msgs.Children.Add(card);
        ScrollEnd();
        Sounds.Play("alert");
    }

    void ApprovalDone(JsonNode ev)
    {
        if (!_approvals.TryGetValue(ev.S("call_id"), out var card)) return;
        var d = ev.S("decision");
        card.BorderBrush = d is "approve" or "always" or "auto" ? Ui.Divider : Ui.Red;
        if (card.Child is StackPanel sp && sp.Children.Count > 0)
        {
            sp.Children.RemoveAt(sp.Children.Count - 1);       // the buttons
            var label = d switch
            {
                "approve" => "approved",
                "always" => "approved, and from now on",
                "auto" => "approved by your settings",
                _ => "rejected" + (ev.S("note").Length > 0 && ev.S("note") != "cancelled" ? ": " + ev.S("note") : ""),
            };
            sp.Children.Add(Ui.T(label, null, 12.5, d == "reject" ? Ui.Red : Ui.Brand, FontWeight.SemiBold));
            if (sp.Children[0] is Grid g && g.Children.Count > 1) g.Children.RemoveAt(1);
        }
    }

    async Task Decide(string callId, string decision, string note)
    {
        if (_run == null) return;
        await Ops.Act("/api/agent/approve", new JsonObject
        {
            ["run"] = _run, ["call_id"] = callId, ["decision"] = decision, ["note"] = note,
        }, null);
    }

    async Task RejectWithNote(string callId)
    {
        var note = await Dialogs.Prompt("Reject", "Tell the assistant why, or what to do instead (optional)", "", "Reject");
        if (note == null) return;
        await Decide(callId, "reject", note);
    }

    void PaintPlan(JsonNode ev)
    {
        _plan.Children.Clear();
        var steps = ev.Strings("steps");
        var done = ev.I("done");
        for (var i = 0; i < steps.Count; i++)
        {
            var finished = i < done;
            var current = i == done;
            _plan.Children.Add(Ui.Lead(Icons.Make(finished ? "circle-check" : current ? "chevron-right" : "info", 14,
                    finished ? Ui.Brand : current ? Ui.Orange : Ui.Secondary),
                Ui.T(steps[i], null, 12.5, finished ? Ui.Secondary : Ui.Text, wrap: true), 8));
        }
        var files = ev.Strings("files");
        if (files.Count > 0)
        {
            _plan.Children.Add(Ui.Eyebrow("Files it expects to change").Also(t => t.Margin = new Thickness(0, 8, 0, 2)));
            foreach (var f in files)
            {
                var path = f;
                _plan.Children.Add(Ui.Btn(f, () => OpenFile(path, 0), "file-text", "ghost xs"));
            }
        }
    }

    void PaintActivity(string state, string detail)
    {
        _activity.Children.Clear();
        var icon = Icons.Make(state == "waiting" ? "alert" : "refresh", 14, state == "waiting" ? Ui.Orange : Ui.Brand);
        if (state != "waiting")
        {
            icon.Classes.Add("spin");
            icon.RenderTransform = new RotateTransform();
        }
        _activity.Children.Add(Ui.Lead(icon, Ui.T(_status.Text, null, 12.5, Ui.Text, wrap: true), 8));
    }

    void ScrollEnd() => Dispatcher.UIThread.Post(() => _msgScroll.ScrollToEnd(), DispatcherPriority.Background);

    // ================================================================ small actions

    async Task AddWorkspace()
    {
        var picked = await S.StorageProvider.OpenFolderPickerAsync(new Avalonia.Platform.Storage.FolderPickerOpenOptions
        {
            Title = "Choose a project folder (a plugin, mod or datapack)",
            AllowMultiple = false,
        });
        if (picked.Count == 0) return;
        var path = picked[0].TryGetLocalPath();
        if (string.IsNullOrEmpty(path)) return;
        var j = await Ops.Act("/api/agent/workspace/add", new JsonObject { ["root"] = path });
        if (j == null) return;
        _ws = j.Path("workspace.id") is { } id ? J.Str(id) : "";
        await LoadState();
        await Open(_ws);
    }

    async Task AddNote()
    {
        if (_ws.Length == 0) return;
        var t = await Dialogs.Prompt("Remember", "A fact about this project the assistant should always know", "", "Save");
        if (string.IsNullOrWhiteSpace(t)) return;
        await Ops.Act("/api/agent/memory/add", new JsonObject { ["ws"] = _ws, ["text"] = t }, null);
        await LoadMemory();
    }

    async Task Forget(string id)
    {
        await Ops.Act("/api/agent/memory/delete", new JsonObject { ["ws"] = _ws, ["id"] = id }, null);
        await LoadMemory();
    }

    async Task Revert(string id, string path)
    {
        if (!await Dialogs.Confirm("Put back " + path, "Restore " + path + " to how it was before this change?", "Put back", "brand"))
            return;
        await Ops.Act("/api/agent/revert", new JsonObject { ["ws"] = _ws, ["change"] = id });
        await LoadChanges();
    }

    async Task Clear()
    {
        if (_ws.Length == 0) return;
        if (!await Dialogs.Confirm("New conversation", "Clear this workspace's conversation? Saved notes and the project index stay.",
                "Clear")) return;
        await Ops.Act("/api/agent/clear", new JsonObject { ["ws"] = _ws });
        ShowIntro();
    }

    async Task Reindex()
    {
        if (_ws.Length == 0) return;
        var j = await Ops.Act("/api/agent/index", new JsonObject { ["ws"] = _ws }, null);
        if (j != null)
            Toasts.Good("Indexed " + Ui.Plural(j.Path("index.files") is { } f ? long.Parse(J.Str(f)) : 0, "file") +
                        (j.Path("index.changed") is { } c ? " (" + J.Str(c) + " changed)" : "") + ".");
    }

    async void Attach()
    {
        var files = await S.StorageProvider.OpenFilePickerAsync(new Avalonia.Platform.Storage.FilePickerOpenOptions
        {
            Title = "Attach a file for the assistant",
            AllowMultiple = false,
        });
        if (files.Count == 0) return;
        try
        {
            await using var s = await files[0].OpenReadAsync();
            using var rd = new StreamReader(s);
            var buf = new char[200_000];
            var n = await rd.ReadBlockAsync(buf, 0, buf.Length);
            _atts.Add(new JsonObject { ["name"] = files[0].Name, ["content"] = new string(buf, 0, n) });
            RenderAtts();
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
    }

    void RenderAtts()
    {
        _attRow.Children.Clear();
        for (var i = 0; i < _atts.Count; i++)
        {
            var idx = i;
            var chip = Ui.Btn(_atts[i].S("name") + "  ×", () =>
            {
                _atts.RemoveAt(idx);
                RenderAtts();
            }, "paperclip", "xs round");
            chip.Margin = new Thickness(0, 0, 6, 0);
            _attRow.Children.Add(chip);
        }
        _attRow.IsVisible = _atts.Count > 0;
    }

    // ================================================================ model & resources

    async Task LoadSettings()
    {
        _settingsView.Children.Clear();
        _settingsView.Children.Add(Ui.Empty("asking Ollama…", "cpu"));
        await LoadState();
        await LoadOllama(true);
        PaintSettings();
    }

    void PaintSettings()
    {
        var st = _state.N("settings") ?? new JsonObject();
        var ol = _ollama ?? new JsonObject();
        _settingsView.Children.Clear();

        // ---- Ollama
        var baseIn = Ui.Input(st.S("base_url"), "http://localhost:11434", mono: true);
        var olStatus = ol.B("running")
            ? Ui.Pill("running · " + ol.S("version"), "on")
            : Ui.Pill("not reachable", "bad");
        _settingsView.Children.Add(Ui.Card("Ollama", "The local model server. Free, open source, runs on this computer.",
            Ui.H(8, olStatus, Ui.Btn("Check", () => _ = LoadSettings(), "refresh", "sm")),
            Ui.V(8, Ui.Field("Address", baseIn), ol.B("running") ? null :
                Ui.T(ol.S("error"), null, 12.5, Ui.Red, wrap: true))));

        // ---- models
        var chosen = st.S("model");
        var toolModes = st.N("tool_mode") as JsonObject ?? new JsonObject();
        var modeBoxes = new Dictionary<string, ComboBox>();
        var models = Ui.V(8);
        string selected = chosen;
        var rows = new List<(string name, Border row)>();
        foreach (var m in ol.A("models").Items())
        {
            var name = m.S("name");
            var tags = Ui.H(6);
            tags.Children.Add(m.S("mode") == "native" ? Ui.Tag("tool calling", "check", Ui.Brand) : Ui.Tag("JSON fallback", "info", Ui.Orange));
            if (m.B("thinking")) tags.Children.Add(Ui.Tag("thinking"));
            if (m.A("capabilities").Items().Any(c => J.Str(c) == "vision")) tags.Children.Add(Ui.Tag("vision"));
            if (m.L("context_length") > 0) tags.Children.Add(Ui.Tag(m.L("context_length") / 1024 + "K context"));
            var info = (m.S("parameter_size") + " " + m.S("quantization")).Trim() + " · " + (m.L("size") / 1073741824.0).ToString("0.0") + " GB";
            var modeSel = Ui.Select(new[] { ("auto", "Tools: automatic"), ("native", "Force native tools"), ("json", "Force JSON mode") },
                toolModes[name] is { } tm ? J.Str(tm) : "auto", width: 190);
            modeBoxes[name] = modeSel;
            var row = Ui.Row(Ui.V(4, Ui.H(8, Ui.T(name, "h3"), Ui.T(info, null, 12, Ui.Secondary)), tags), modeSel);
            row.Cursor = new Cursor(StandardCursorType.Hand);
            row.Classes.Set("active", name == chosen);
            rows.Add((name, row));
            row.PointerPressed += (_, e) =>
            {
                if (e.Source is Visual v && v.FindAncestorOfType<ComboBox>(true) != null) return;
                selected = name;
                foreach (var (n, r) in rows) r.Classes.Set("active", n == name);
                _ = Recommend(name);
            };
            models.Children.Add(row);
        }
        if (models.Children.Count == 0)
            models.Children.Add(Ui.Empty(ol.B("running")
                ? "No models installed. In a terminal: ollama pull qwen2.5-coder:7b (or any model marked 'tools' at ollama.com/search?c=tool)."
                : "Start Ollama to see its models.", "box"));
        _settingsView.Children.Add(Ui.Card("Model", "Click one to use it. Models that support tool calling work best; others get a JSON reply format.",
            null, models));

        // ---- context and resources
        var ctx = Ui.Input(st.S("num_ctx"), "8192", mono: true, width: 140).Also(Left);
        _recommend = Ui.V(6);
        var temp = Ui.Input(st.S("temperature"), "0.2", mono: true, width: 100).Also(Left);
        var keep = Ui.Select(new[]
        {
            ("", "Ollama's default (5 minutes)"), ("30m", "30 minutes"), ("2h", "2 hours"), ("-1", "Until Ollama stops"),
            ("0", "Unload after every request"),
        }, st.S("keep_alive"), width: 260);
        var threads = Ui.Input(st.S("num_thread"), "automatic", mono: true, width: 140).Also(Left);
        var gpu = Ui.Input(st.S("num_gpu"), "automatic", mono: true, width: 140).Also(Left);
        var parallel = Ui.Select(new[] { ("1", "1 (recommended)"), ("2", "2"), ("3", "3"), ("4", "4") }, st.S("max_parallel", "1"), width: 160);
        var steps = Ui.Input(st.S("max_steps"), "24", mono: true, width: 100).Also(Left);
        var think = Ui.Switch("Let thinking models think first", st.B("think"));
        _settingsView.Children.Add(Ui.Card("Context and resources",
            "Bigger is not automatically faster: the context's memory grows with its size, and a model that no longer fits the GPU runs on the CPU.",
            null, Ui.V(4,
                Ui.Cols(16,
                    Ui.Field("Context length (num_ctx)", ctx, "Tokens the model can see at once. Every request asks for the same value, so Ollama never reloads the model for it."),
                    Ui.Field("Temperature", temp, "Low keeps code edits steady.")),
                _recommend,
                Ui.Cols(16,
                    Ui.Field("Keep the model loaded", keep, "Longer avoids reloading between requests; shorter frees memory sooner."),
                    Ui.Field("Assistant tasks at once", parallel, "More than one makes them share the model and the GPU.")),
                Ui.Cols(16,
                    Ui.Field("CPU threads (num_thread)", threads, "Blank lets Ollama pick. Advanced."),
                    Ui.Field("GPU layers (num_gpu)", gpu, "Blank lets Ollama pick; 0 forces the CPU. Advanced.")),
                Ui.Cols(16, Ui.Field("Steps per task", steps, "How many tool calls a task may make before it stops and asks."),
                    Ui.SwitchHint(think, "Only for models marked 'thinking'. Slower, often better on hard problems.")))));

        // ---- reach: files, shell, approvals
        var access = Ui.Select(new[]
        {
            ("workspace", "Only the workspace folder"), ("computer", "This whole computer (system folders excepted)"),
        }, st.S("access", "workspace"), width: 380);
        var approval = Ui.Select(new[]
        {
            ("ask", "Ask before every change, command and server action"), ("edits", "Make file changes, ask for the rest"),
            ("all", "Never ask - let it act on its own"),
        }, st.S("approval_mode", "ask"), width: 380);
        var shell = Ui.Switch("Let it run shell commands (" + (OperatingSystem.IsWindows() ? "PowerShell" : "bash") + ")", st.B("shell_enabled"));
        var reachWarn = Ui.T("", null, 12.5, Ui.Orange, wrap: true);
        void Warn()
        {
            var parts = new List<string>();
            if (Ui.Val(access) == "computer") parts.Add("It can read any file on this computer, and change files outside the system folders.");
            if (shell.IsChecked == true) parts.Add("A shell command can do anything you can do in a terminal.");
            if (Ui.Val(approval) == "all") parts.Add("Nothing waits for you: it acts as soon as it decides to. File changes are still backed up.");
            reachWarn.Text = string.Join(" ", parts);
            reachWarn.IsVisible = parts.Count > 0;
        }
        access.SelectionChanged += (_, _) => Warn();
        approval.SelectionChanged += (_, _) => Warn();
        shell.IsCheckedChanged += (_, _) => Warn();
        Warn();
        var allowed =Ui.Area(string.Join("\n", st.Strings("allowed_commands")), 3, "one per line, e.g. npm test", true);
        var trusted = Ui.V(4);
        foreach (var c in st.Strings("trusted_commands"))
        {
            var cmd = c;
            trusted.Children.Add(Ui.Split(Ui.MonoText(c, 12.5), Ui.IconBtn("x", "Ask again before running it", () => _ = Untrust(cmd), "ghost xs", 24)));
        }
        if (trusted.Children.Count == 0) trusted.Children.Add(Ui.Hint("None. Choose 'Always allow this command' on a command to add it here."));
        _settingsView.Children.Add(Ui.Card("Access and approvals",
            "How far the assistant reaches, and what it may do without asking first. It can always start, stop and talk to your servers - with your approval unless you turn asking off.",
            null, Ui.V(8,
                Ui.Cols(16,
                    Ui.Field("Files it can reach", access, "With the whole computer, a \"This computer\" workspace appears in the list."),
                    Ui.Field("Approvals", approval, "Every file change is backed up and can be put back, whatever you pick.")),
                Ui.SwitchHint(shell, "Tests, scripts, installs, git - anything a terminal can do. Each command is shown before it runs unless approvals are off."),
                reachWarn,
                Ui.Field("Commands it may also run without the shell (still asked each time)", allowed,
                    "By default: gradle, gradlew, mvn, mvnw, java/javac -version and read-only git."),
                Ui.Label("Commands that run without asking"), trusted)));

        // ---- web
        var web = Ui.Switch("Let it search the web and read pages", st.B("web_enabled"));
        var provider = Ui.Select(new[]
        {
            ("none", "Reading pages only (no search)"), ("ollama", "Ollama web search (free account)"), ("searxng", "SearXNG (self-hosted, free)"),
        }, st.S("web_provider", "none"), width: 320);
        var hasKey = st.B("has_ollama_api_key");
        var apiKey = Ui.Input("", hasKey ? "saved - type a new key to replace it" : "paste your key", password: true, mono: true, width: 380).Also(Left);
        var searx = Ui.Input(st.S("searxng_url"), "http://localhost:8080", mono: true, width: 380).Also(Left);
        var keyField = Ui.Field("Ollama API key", apiKey, "Free: sign in at ollama.com and create one under Settings > Keys. Kept on this computer only.");
        var searxField = Ui.Field("SearXNG address", searx, "Your instance must allow the json format (search: formats: [html, json] in settings.yml).");
        var hasGh = st.B("has_github_token");
        var ghToken = Ui.Input("", hasGh ? "saved - type a new token to replace it" : "optional", password: true, mono: true, width: 380).Also(Left);
        var ghField = Ui.Field("GitHub token", Ui.H(8, ghToken, hasGh ? Ui.Btn("Forget", async () =>
            {
                if (await Ops.Act("/api/agent/settings", new JsonObject { ["github_token_clear"] = true }, "Token removed.") != null) await LoadSettings();
            }, "trash", "ghost sm") : null),
            "Reading public GitHub repositories works without one (60 requests an hour). Any token - even with no scopes, from github.com/settings/tokens - raises that to 5000 and lets it search code.");
        void ShowProvider()
        {
            keyField.IsVisible = Ui.Val(provider) == "ollama";
            searxField.IsVisible = Ui.Val(provider) == "searxng";
        }
        provider.SelectionChanged += (_, _) => ShowProvider();
        ShowProvider();
        var clearKey = hasKey ? Ui.Btn("Forget the key", async () =>
        {
            if (await Ops.Act("/api/agent/settings", new JsonObject { ["ollama_api_key_clear"] = true }, "Key removed.") != null) await LoadSettings();
        }, "trash", "ghost sm") : null;
        _settingsView.Children.Add(Ui.Card("Web", "Searching needs a search service - both choices here are free. Reading a page you or the assistant name works without one. Whatever a page says is passed on as information, never as instructions.",
            clearKey, Ui.V(8, Ui.SwitchHint(web, "Reading a page waits for your approval unless approvals are off."),
                Ui.Field("Search with", provider), keyField, searxField, ghField)));

        // ---- loaded models
        var loaded = Ui.V(6);
        foreach (var m in ol.A("loaded").Items())
        {
            var name = m.S("name");
            var vram = m.L("size_vram");
            var size = m.L("size");
            var where = vram <= 0 ? "on the CPU" : vram >= size ? "fully on the GPU" : (100.0 * vram / Math.Max(1, size)).ToString("0") + "% on the GPU";
            loaded.Children.Add(Ui.Row(Ui.TitleSub(name, (size / 1073741824.0).ToString("0.0") + " GB · " + where +
                    (m.L("context_length") > 0 ? " · context " + m.S("context_length") : "") + " · until " + m.S("expires_at")),
                Ui.Btn("Unload", () => _ = ModelAction("unload", name), null, "sm")));
        }
        if (loaded.Children.Count == 0) loaded.Children.Add(Ui.Hint("No model is loaded right now."));
        _settingsView.Children.Add(Ui.Card("Loaded right now", "From Ollama's own list of running models.",
            Ui.Btn("Load the chosen model", () => _ = ModelAction("preload", ""), "play", "sm",
                "Load it now with the assistant's settings, so the first message does not wait"), loaded));

        // ---- save
        var save = Ui.Btn("Save settings", async () =>
        {
            var tm = new JsonObject();
            foreach (var (n, box) in modeBoxes) tm[n] = Ui.Val(box) ?? "auto";
            var body = new JsonObject
            {
                ["base_url"] = baseIn.Text ?? "", ["model"] = selected, ["num_ctx"] = ctx.Text ?? "",
                ["temperature"] = temp.Text ?? "0.2", ["keep_alive"] = Ui.Val(keep) ?? "", ["num_thread"] = threads.Text ?? "",
                ["num_gpu"] = gpu.Text ?? "", ["max_parallel"] = Ui.Val(parallel) ?? "1", ["max_steps"] = steps.Text ?? "24",
                ["think"] = think.IsChecked == true, ["approval_mode"] = Ui.Val(approval) ?? "ask",
                ["access"] = Ui.Val(access) ?? "workspace", ["shell_enabled"] = shell.IsChecked == true,
                ["web_enabled"] = web.IsChecked == true, ["web_provider"] = Ui.Val(provider) ?? "none",
                ["searxng_url"] = searx.Text ?? "",
                ["allowed_commands"] = allowed.Text ?? "", ["tool_mode"] = tm,
            };
            if ((apiKey.Text ?? "").Trim().Length > 0) body["ollama_api_key"] = apiKey.Text!.Trim();
            if ((ghToken.Text ?? "").Trim().Length > 0) body["github_token"] = ghToken.Text!.Trim();
            if (await Ops.Act("/api/agent/settings", body, "Saved.") != null) await LoadSettings();
        }, "save", "brand");
        _settingsView.Children.Insert(0, Ui.Split(Ui.V(2, Ui.H1("Model & resources"),
            Ui.Hint("How the assistant uses Ollama on this computer.")), save));
        if (selected.Length > 0) _ = Recommend(selected);
    }

    StackPanel _recommend = new();

    static void Left(Control c) => c.HorizontalAlignment = HorizontalAlignment.Left;

    async Task Recommend(string model)
    {
        var j = await Ops.Get("/api/agent/recommend?model=" + Api.Q(model), false);
        _recommend.Children.Clear();
        if (j == null) return;
        var free = j.L("free_bytes");
        _recommend.Children.Add(Ui.T("For " + model + " on this computer: " + j.S("recommended") + " tokens" +
            (free > 0 ? " (" + (free / 1073741824.0).ToString("0.0") + " GB free " + (j.B("on_gpu") ? "on the GPU" : "memory") + ")" : "") +
            ". The model was trained for up to " + j.S("context_length", "?") + ".", null, 12.5, Ui.Text, wrap: true));
        var table = Ui.H(14);
        foreach (var r in j.A("table").Items())
            table.Children.Add(Ui.V(0, Ui.T(r.S("num_ctx"), null, 12.5, Ui.Contrast, FontWeight.SemiBold), Ui.T(r.S("kv"), null, 11.5, Ui.Secondary)));
        _recommend.Children.Add(table);
        _recommend.Children.Add(Ui.Hint(j.S("note")));
    }

    async Task Untrust(string cmd)
    {
        var list = new JsonArray();
        foreach (var c in (_state.N("settings") ?? new JsonObject()).Strings("trusted_commands"))
            if (c != cmd) list.Add(c);
        await Ops.Act("/api/agent/settings", new JsonObject { ["trusted_commands"] = list }, "Removed.");
        await LoadSettings();
    }

    async Task ModelAction(string action, string model)
    {
        var j = await Ops.Act("/api/agent/model", new JsonObject { ["action"] = action, ["model"] = model }, "");
        if (j != null) await LoadSettings();
    }
}
