using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// Log search, memory, worlds, plugins and the ops/whitelist/ban files - five
/// cards the old page stacked, here behind Modrinth's pill tabs.
/// </summary>
public sealed class ToolsPage : Page
{
    public ToolsPage(MainWindow s) : base(s) { }

    public override string Key => "tools";
    public override string Group => "Manage";
    public override string Title => "Tools";
    public override string Icon => "wrench";
    public override bool Advanced => true;

    string _tab = "logs";
    ContentControl _body = null!;
    readonly Dictionary<string, Control> _tabs = new();

    // log search
    TextBox _q = null!;
    ToggleButton _regex = null!, _case = null!;
    StackPanel _lsOut = null!;
    // memory
    TextBlock _ramSummary = null!;
    StackPanel _ramOut = null!;
    JsonNode? _ram;
    // worlds / plugins
    TextBlock _wCount = null!, _plCount = null!;
    StackPanel _wList = null!, _plList = null!;
    // lists
    ComboBox _which = null!;
    TextBox _name = null!;
    StackPanel _listOut = null!;

    protected override Control Build()
    {
        var (bar, select) = Ui.NavTabs(new (string, string, string?)[]
        {
            ("logs", "Log search", "search"), ("ram", "Memory", "cpu"), ("worlds", "Worlds", "mountain"),
            ("plugins", "Plugins", "puzzle"), ("lists", "Player lists", "list"),
        }, _tab, k =>
        {
            _tab = k;
            ShowTab();
            _ = LoadTab();
        });
        bar.Margin = new Thickness(0, 0, 0, 16);
        _selectTab = select;

        // ---- logs
        _q = Ui.Input("", "Exception, a player name, or Can't keep up").OnEnter(() => _ = Search());
        _q.InnerLeftContent = Icons.Make("search", 16, Ui.Secondary).Also(i => i.Margin = new Thickness(12, 0, 0, 0));
        _regex = Ui.Switch("regex", false);
        _case = Ui.Switch("match case", false);
        _lsOut = Ui.V(10);
        var qRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        qRow.Children.Add(_q);
        var go = Ui.Btn("Search", () => _ = Search(), "search", "brand");
        go.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(go, 1);
        qRow.Children.Add(go);
        _tabs["logs"] = Ui.Card("Search the logs", "Including the gzipped rotated ones",
            Ui.H(16, _regex, _case),
            Ui.V(10, qRow,
                Ui.Hint("Remote servers pull each log down over SFTP, so this is capped at the twelve most recent files and 400 hits."),
                _lsOut));

        // ---- memory
        _ramSummary = Ui.Hint("—");
        _ramOut = Ui.V(8);
        _tabs["ram"] = Ui.Card(Ui.V(16,
            Ui.Split(Ui.V(3, Ui.H2("Free memory"), _ramSummary), Ui.Btn("Refresh", () => _ = LoadRam(), "refresh", "sm")),
            _ramOut,
            Ui.Hint("Closing is a normal close request, the kind the application can refuse if it has unsaved work - not a kill. " +
                    "Windows components, Lodestone and every Java process are never offered, because a Java process here is " +
                    "either a Minecraft server or the game. Stop servers with their own Stop button so the world saves first.")));

        // ---- worlds
        _wCount = Ui.Hint("—");
        _wList = Ui.V(8);
        _tabs["worlds"] = Ui.Card(Ui.V(16,
            Ui.Split(Ui.V(3, Ui.H2("Worlds"), _wCount), Ui.Btn("Refresh", () => _ = LoadWorlds(), "refresh", "sm")),
            _wList,
            Ui.Hint("The live world is whatever level-name points at. Switching takes effect on the next restart. Resetting clears " +
                    "the chunks so the server regenerates them; the server has to be stopped first.")));

        // ---- plugins
        _plCount = Ui.Hint("—");
        _plList = Ui.V(8);
        _tabs["plugins"] = Ui.Card(Ui.V(16,
            Ui.Split(Ui.V(3, Ui.H2("Plugins"), _plCount), Ui.Btn("Refresh", () => _ = LoadPlugins(), "refresh", "sm")),
            _plList));

        // ---- lists
        _which = Ui.Select(new[]
        {
            ("ops", "Operators"), ("whitelist", "Whitelist"), ("banned-players", "Banned players"), ("banned-ips", "Banned IPs"),
        }, "ops", v => { _ = LoadList(); }, 180);
        _name = Ui.Input("", "Player name", width: 180).OnEnter(() => _ = ListEdit(true));
        _listOut = Ui.V(8);
        _tabs["lists"] = Ui.Card(Ui.V(16,
            Ui.Split(Ui.H2("Ops, whitelist and bans"),
                Ui.H(8, _which, _name, Ui.Btn("Add", () => _ = ListEdit(true), "plus", "brand sm"),
                    Ui.Btn("Remove", () => _ = ListEdit(false), "trash", "soft-red sm"))),
            _listOut,
            Ui.Hint("These are read from the server's JSON files, and changed by sending the real command - so the server has to " +
                    "be running, or reachable over RCON.")));

        _body = new ContentControl();
        ShowTab();
        return Ui.Scroll(Ui.V(0, bar, _body), new Thickness(28, 16, 28, 28));
    }

    void ShowTab() => _body.Content = _tabs[_tab];

    Action<string>? _selectTab;

    /// <summary>For --shots: open a tab as if its pill were clicked.</summary>
    public async Task OpenTab(string key)
    {
        _tab = key;
        _selectTab?.Invoke(key);
        ShowTab();
        await LoadTab();
    }

    public override Task OnShow() => LoadTab();

    Task LoadTab() => _tab switch
    {
        "ram" => LoadRam(),
        "worlds" => LoadWorlds(),
        "plugins" => LoadPlugins(),
        "lists" => LoadList(),
        _ => Task.CompletedTask,
    };

    // ================================================================ logs

    async Task Search()
    {
        var q = (_q.Text ?? "").Trim();
        if (q.Length == 0) return;
        _lsOut.Children.Clear();
        _lsOut.Children.Add(Ui.Empty("searching…"));
        try
        {
            var j = await Api.Post("/api/logs/search", new JsonObject
            {
                ["query"] = q, ["regex"] = _regex.IsChecked == true, ["case"] = _case.IsChecked == true,
            });
            _lsOut.Children.Clear();
            var scanned = j.A("scanned").Items().ToList();
            var hits = j.A("hits").Items().ToList();
            var per = string.Join("  |  ", scanned.Select(x => x.S("name") + (x.S("error").Length > 0 ? " (" + x.S("error") + ")" : " · " + x.S("hits", "0"))));
            _lsOut.Children.Add(Ui.Hint(hits.Count + " hit(s)" + (j.B("truncated") ? " (capped)" : "") + " across " +
                                        scanned.Count + " file(s): " + per));
            if (hits.Count == 0)
            {
                _lsOut.Children.Add(Ui.Empty("Nothing matched.", "search"));
                return;
            }
            var text = new TextBlock { FontFamily = Ui.Mono, FontSize = 12.5, TextWrapping = TextWrapping.Wrap, LineHeight = 19 };
            foreach (var h in hits.Take(400))
            {
                text.Inlines!.Add(new Avalonia.Controls.Documents.Run(h.S("file") + ":" + h.S("line") + "  ") { Foreground = Ui.Secondary });
                text.Inlines.Add(new Avalonia.Controls.Documents.Run(h.S("text") + "\n") { Foreground = Ui.Text });
            }
            var code = new Border { Child = text };
            code.Classes.Add("code");
            _lsOut.Children.Add(code);
        }
        catch (Exception e)
        {
            _lsOut.Children.Clear();
            _lsOut.Children.Add(Ui.Empty(e.Message, "alert"));
        }
    }

    // ================================================================ memory

    static string Gb(double mb) => (mb / 1024).ToString("0.0") + " GB";

    async Task LoadRam()
    {
        JsonNode j;
        try { j = await Api.Get("/api/ram"); }
        catch (Exception e)
        {
            _ramOut.Children.Clear();
            _ramOut.Children.Add(Ui.Hint(e.Message));
            return;
        }
        _ram = j;
        _ramOut.Children.Clear();
        if (j.IsFalse("supported"))
        {
            _ramSummary.Text = "not available";
            _ramOut.Children.Add(Ui.Hint(j.S("error", "Windows only.")));
            return;
        }
        _ramSummary.Text = Gb(j.D("free_mb")) + " free of " + Gb(j.D("total_mb")) + "  ·  " + j.S("percent") + "% used";

        var used = Math.Clamp(j.D("percent") / 100.0, 0, 1);
        var meter = new ProgressBar
        {
            Minimum = 0, Maximum = 1, Value = used, Height = 10, MinHeight = 10, CornerRadius = new CornerRadius(5),
            Foreground = used > 0.85 ? Ui.Red : used > 0.7 ? Ui.Orange : Ui.Brand, Background = Ui.ButtonBg,
        };
        _ramOut.Children.Add(meter);
        _ramOut.Children.Add(Ui.Hint("Closing everything listed as closeable would free about " + Gb(j.D("reclaimable_mb")) +
                                     ". A 100+ mod server wants 6-8 GB."));
        foreach (var p in j.A("processes").Items())
        {
            var mb = p.D("mb");
            var name = p.S("name");
            var pids = p.A("pids").Count;
            _ramOut.Children.Add(Ui.Row(
                Ui.TitleSub(p.S("label", name),
                    (mb >= 1024 ? (mb / 1024).ToString("0.0") + " GB" : mb.ToString("0") + " MB") + " · " +
                    Ui.Plural(pids, "process", "processes") + (p.S("note").Length > 0 ? " · " + p.S("note") : ""),
                    p.B("holds_work") ? Ui.Pill("may have unsaved work", "warn", false) : null,
                    p.B("closeable") ? null : Ui.Pill("protected", "", false)),
                p.B("closeable") ? Ui.Btn("Close", () => RamClose(name), "x", "sm") : null));
        }
    }

    async void RamClose(string name)
    {
        var p = _ram.A("processes").Items().FirstOrDefault(x => x.S("name") == name);
        var mb = p.D("mb");
        var pids = p.A("pids").Count;
        var text = "This frees about " + (mb >= 1024 ? (mb / 1024).ToString("0.0") + " GB" : mb.ToString("0") + " MB") +
                   " across " + Ui.Plural(pids, "process", "processes") + ".";
        if (p.B("holds_work"))
            text += "\n\n" + p.S("label", name) + " may have unsaved work. This sends a normal close request, so it can still " +
                    "prompt you - but save first to be sure.";
        if (!await Dialogs.Confirm("Close " + p.S("label", name) + "?", text, "Close it")) return;
        var j = await Ops.Act("/api/ram/close", new { name }, null);
        if (j == null) return;
        Toasts.Show(j.S("msg") + (j.I("freed_mb") > 0 ? "  (+" + j.S("freed_mb") + " MB free)" : ""), j.B("still_running") ? "" : "good");
        await LoadRam();
    }

    // ================================================================ worlds

    async Task LoadWorlds()
    {
        _wList.Children.Clear();
        JsonNode j;
        try { j = await Api.Get("/api/worlds"); }
        catch (Exception e)
        {
            _wList.Children.Add(Ui.Empty(e.Message, "alert"));
            return;
        }
        if (j.IsFalse("ok"))
        {
            _wList.Children.Add(Ui.Empty(j.S("error"), "alert"));
            return;
        }
        var items = j.A("items").Items().ToList();
        _wCount.Text = items.Count + " world(s)" + (j.S("active").Length > 0 ? " · live: " + j.S("active") : "");
        if (items.Count == 0)
        {
            _wList.Children.Add(Ui.Empty("No worlds found. A world is a folder holding level.dat.", "mountain"));
            return;
        }
        foreach (var w in items)
        {
            var name = w.S("name");
            var row = Ui.Row(
                Ui.Lead(Ui.Rounded(Icons.Make("mountain", 18, w.B("active") ? Ui.Brand : Ui.Secondary), 38, 10),
                    Ui.TitleSub(name, null,
                        w.B("active") ? Ui.Pill("live", "on") : null,
                        w.B("nether") ? Ui.Tag("nether") : null,
                        w.B("end") ? Ui.Tag("end") : null)),
                w.B("active") ? null : Ui.Btn("Make live", () => _ = UseWorld(name), "check", "sm"),
                Ui.Btn("Download", () => _ = S.SaveDownload("/api/download/folder?path=" + Api.Q(w.S("rel")), name + ".zip"), "download", "sm"),
                Ui.Btn("Reset", () => ResetWorld(name, "contents"), "restart", "sm soft-orange"),
                Ui.IconBtn("trash", "Delete the world", () => ResetWorld(name, "all"), "soft-red", 32));
            if (w.B("active")) row.Classes.Add("active");
            _wList.Children.Add(row);
        }
    }

    async Task UseWorld(string name)
    {
        var j = await Ops.Act("/api/worlds/activate", new { name }, null);
        if (j != null) Toasts.Good(j.S("note", "Set."));
        await LoadWorlds();
    }

    async void ResetWorld(string name, string mode)
    {
        var all = mode == "all";
        if (!await Dialogs.Confirm(all ? "Delete the world " + name + "?" : "Reset " + name + "?",
                all ? "The entire folder is removed. Everything built there is gone, and this cannot be undone from the panel."
                    : "Clears region, entities, poi and level.dat so the server regenerates the world. Builds in it are gone. " +
                      "Back up first if you care about it.",
                all ? "Delete" : "Reset"))
            return;
        try
        {
            var j = await Api.Post("/api/worlds/reset", new { name, mode });
            Toasts.Good(j.S("note", "Done."));
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
        await LoadWorlds();
    }

    // ================================================================ plugins

    async Task LoadPlugins()
    {
        _plList.Children.Clear();
        JsonNode j;
        try { j = await Api.Get("/api/plugins"); }
        catch (Exception e)
        {
            _plList.Children.Add(Ui.Empty(e.Message, "alert"));
            return;
        }
        if (j.IsFalse("ok"))
        {
            _plCount.Text = "";
            _plList.Children.Add(Ui.Empty(j.S("error"), "puzzle"));
            return;
        }
        _plCount.Text = j.S("count", "0") + " jar(s)";
        var items = j.A("items").Items().ToList();
        if (items.Count == 0)
        {
            _plList.Children.Add(Ui.Empty("No plugin jars here.", "puzzle"));
            return;
        }
        foreach (var x in items)
        {
            var rel = x.S("rel");
            var title = x.S("title");
            var on = x.B("enabled");
            var row = Ui.Row(
                Ui.Lead(Ui.Rounded(Icons.Make("puzzle", 18, on ? Ui.Brand : Ui.Secondary), 38, 10),
                    Ui.TitleSub(title, x.S("size") + " · " + x.S("mod"), on ? null : Ui.Pill("disabled", "", false))),
                Ui.Switch(null, on, v => { _ = TogglePlugin(rel); }).Also(t => ToolTip.SetTip(t, on ? "Disable" : "Enable")),
                Ui.IconBtn("download", "Download", () => _ = S.SaveDownload("/api/download?path=" + Api.Q(rel), rel.Split('/').Last()), "ghost", 32),
                Ui.IconBtn("trash", "Delete", () => DelPlugin(rel, title), "soft-red", 32));
            row.Opacity = on ? 1 : 0.7;
            _plList.Children.Add(row);
        }
    }

    async Task TogglePlugin(string rel)
    {
        var j = await Ops.Act("/api/plugins/toggle", new { path = rel }, null);
        if (j != null) Toasts.Good(j.S("note", "Done."));
        await LoadPlugins();
    }

    async void DelPlugin(string rel, string name)
    {
        if (!await Dialogs.Confirm("Delete " + name + "?", "The jar is removed from the server. Its config folder is left alone.", "Delete"))
            return;
        await Ops.Act("/api/file/delete", new { path = rel }, "Deleted.");
        await LoadPlugins();
    }

    // ================================================================ lists

    async Task LoadList()
    {
        _listOut.Children.Clear();
        try
        {
            var j = await Api.Get("/api/list?which=" + Api.Q(Ui.Val(_which) ?? "ops"));
            var items = j.A("items").Items().ToList();
            if (items.Count == 0)
            {
                _listOut.Children.Add(Ui.Empty("Empty. If the server has never written this file it will not exist yet.", "list"));
                return;
            }
            foreach (var x in items)
            {
                var n = x.S("name", x.S("uuid", x.S("ip", x.ToJsonString())));
                var extra = string.Join(" · ", new[]
                {
                    x.S("level").Length > 0 ? "level " + x.S("level") : "",
                    x.S("reason").Length > 0 ? "reason: " + x.S("reason") : "",
                    x.S("source").Length > 0 ? "by " + x.S("source") : "",
                    x.S("expires").Length > 0 && x.S("expires") != "forever" ? "until " + x.S("expires") : "",
                }.Where(s => s.Length > 0));
                var who = n;
                _listOut.Children.Add(Ui.Row(
                    Ui.Lead(Images.Slot(Images.Head(n), 34, 8), Ui.TitleSub(n, extra.Length > 0 ? extra : null)),
                    Ui.Btn("Remove", () => { _name.Text = who; _ = ListEdit(false); }, "trash", "sm soft-red")));
            }
        }
        catch (Exception e)
        {
            _listOut.Children.Add(Ui.Empty(e.Message, "alert"));
        }
    }

    async Task ListEdit(bool add)
    {
        var player = (_name.Text ?? "").Trim();
        if (player.Length == 0)
        {
            Toasts.Bad("Type a name first.");
            return;
        }
        try
        {
            var j = await Api.Post("/api/list/edit", new { which = Ui.Val(_which), player, add });
            Toasts.Good(j.S("out", "Done."));
            _name.Text = "";
            await Task.Delay(900);
            await LoadList();
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
    }
}
