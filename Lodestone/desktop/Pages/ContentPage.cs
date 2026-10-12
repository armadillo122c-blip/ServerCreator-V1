using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// Mods or plugins for the server on screen - never both, because a server
/// loads one or the other. Installed: what is in mods/ or plugins/, with
/// updates, enable/disable and remove. Browse: Modrinth, Hangar and SpigotMC,
/// filtered to what this server can load.
/// </summary>
public sealed class ContentPage : Page
{
    public ContentPage(MainWindow s) : base(s) { }

    public override string Key => "content";
    public override string Group => "Manage";
    public override string Title => "Mods & plugins";
    public override string Icon => "puzzle";

    string _tab = "installed";
    JsonNode? _profile;
    ContentControl _body = null!;
    Action<string>? _selectTab;
    TextBlock _heading = null!, _about = null!, _count = null!;
    StackPanel _list = null!;
    Button _checkBtn = null!, _updateAll = null!;
    Control _installedView = null!;
    ContentBrowser _browser = null!;
    readonly Dictionary<string, JsonNode> _updates = new();
    string _loadedFor = "";
    List<JsonNode> _items = new();
    string _filter = "all";
    TextBox _find = null!;
    ComboBox _sortPick = null!;
    WrapPanel _chips = null!, _stats = null!;

    string Sid => S.State.N("server").S("id");

    protected override Control Build()
    {
        _heading = Ui.H2("Mods & plugins");
        _about = Ui.Hint("");
        var (bar, select) = Ui.NavTabs(new (string, string, string?)[]
        {
            ("installed", "Installed", "check"), ("browse", "Browse", "search"),
        }, _tab, k =>
        {
            _tab = k;
            ShowTab();
        });
        _selectTab = select;

        _count = Ui.Hint("—");
        _list = Ui.V(8);
        _checkBtn = Ui.Btn("Check for updates", () => _ = CheckUpdates(), "refresh", "sm");
        _updateAll = Ui.Btn("Update all", () => _ = UpdateAll(), "download", "brand sm");
        _updateAll.IsVisible = false;
        _find = Ui.Input("", "Find by name, file or source");
        _find.InnerLeftContent = Icons.Make("search", 16, Ui.Secondary).Also(i => i.Margin = new Thickness(12, 0, 0, 0));
        _find.TextChanged += (_, _) => Render();
        _sortPick = Ui.Select(new[] { ("name", "Sort: name"), ("recent", "Sort: recently installed"), ("size", "Sort: size"), ("source", "Sort: source") },
            S.Prefs.ContentSort, v =>
            {
                S.Prefs.ContentSort = v;
                S.Prefs.Save();
                Render();
            }, 220);
        _chips = new WrapPanel();
        _stats = new WrapPanel();
        var findRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        findRow.Children.Add(_find);
        _sortPick.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(_sortPick, 1);
        findRow.Children.Add(_sortPick);
        _installedView = Ui.V(14, _stats, Ui.Card(Ui.V(14,
            Ui.Split(Ui.V(3, Ui.H2("Installed"), _count),
                Ui.H(8, _updateAll, _checkBtn, Ui.IconBtn("refresh", "Reload the list", () => _ = LoadInstalled(), "ghost", 32))),
            findRow, _chips,
            _list,
            Ui.Hint("Disabling renames the jar to .jar.disabled, so nothing is lost. Removing deletes the jar but keeps its config " +
                    "folder. Changes take effect the next time the server starts."))));

        _browser = new ContentBrowser(() => "sid=" + Api.Q(Sid), Install);

        _body = new ContentControl();
        var head = Ui.Split(Ui.V(3, _heading, _about), bar);
        return Ui.Scroll(Ui.V(16, head, _body), new Thickness(28, 16, 28, 28));
    }

    void ShowTab()
    {
        if (_profile != null && _profile.S("kind").Length == 0)
        {
            _body.Content = Ui.Card(Ui.V(12,
                Ui.Empty(_profile.S("reason", "This server takes no mods or plugins."), "puzzle"),
                Ui.H(8, Ui.Btn("Create a modded or plugin server", () => _ = S.NewServer(), "plus", "sm"))
                    .Also(h => h.HorizontalAlignment = HorizontalAlignment.Center)));
            return;
        }
        if (_tab == "browse")
        {
            _body.Content = Ui.Card(_browser.View);
            if (_profile != null && _loadedFor != Sid + "|browse")
            {
                _loadedFor = Sid + "|browse";
                _ = _browser.SetProfile(_profile);
            }
        }
        else _body.Content = _installedView;
    }

    /// <summary>For --shots.</summary>
    public async Task OpenTab(string key)
    {
        _tab = key;
        _selectTab?.Invoke(key);
        ShowTab();
        if (key == "installed") await LoadInstalled();
    }

    public override async Task OnShow()
    {
        await LoadInstalled();
        ShowTab();
    }

    public override void OnServerSwitched()
    {
        _profile = null;
        _loadedFor = "";
        _updates.Clear();
        _items.Clear();
        _filter = "all";
        if (Built) _list.Children.Clear();
    }

    async Task LoadInstalled()
    {
        JsonNode j;
        try { j = await Api.Get("/api/content/installed?sid=" + Api.Q(Sid)); }
        catch (Exception e)
        {
            _list.Children.Clear();
            _list.Children.Add(Ui.Empty(e.Message, "alert"));
            return;
        }
        var prof = j.N("profile");
        var changed = _profile == null || _profile.S("software") != prof.S("software") || _profile.S("mc") != prof.S("mc");
        _profile = prof;
        if (changed) _loadedFor = "";
        var kind = prof.S("kind");
        _heading.Text = kind == "mod" ? "Mods" : kind == "plugin" ? "Plugins" : "Mods & plugins";
        _about.Text = kind.Length == 0 ? prof.S("server")
            : prof.S("label") + (prof.S("mc").Length > 0 ? " " + prof.S("mc") : " (version unknown)") + "  ·  " +
              (kind == "mod" ? "mods" : "plugins") + " go in " + prof.S("dir") + "/" +
              (prof.S("detected").Length > 0 && prof.S("detected") != "created by Lodestone" ? "  ·  detected: " + prof.S("detected") : "");
        if (kind.Length == 0)
        {
            ShowTab();
            return;
        }
        _items = j.A("items").Items().ToList();
        Render();
    }

    static bool Problem(JsonNode x) => x.A("problems").Count > 0;

    bool Pass(JsonNode x, string f) => f switch
    {
        "attention" => Problem(x) || _updates.ContainsKey(x.S("file")),
        "yours" => !x.B("auto"),
        "deps" => x.B("auto"),
        "disabled" => !x.B("enabled"),
        _ => true,
    };

    /// <summary>Stats, chips, then the list in sections - all from the last load, no engine call.</summary>
    void Render()
    {
        if (_profile == null || _list == null) return;
        var items = _items;
        var what = _profile.S("kind") == "mod" ? "mod" : "plugin";
        _count.Text = Ui.Plural(items.Count, what) + " in " + _profile.S("dir") + "/" +
                      (_updates.Count > 0 ? "  ·  " + Ui.Plural(_updates.Count, "update") + " available" : "");
        _updateAll.IsVisible = _updates.Count > 0;
        _updateAll.Content = Ui.H(7, Icons.Make("download", 15), new TextBlock { Text = "Update all (" + _updates.Count + ")", VerticalAlignment = VerticalAlignment.Center });

        // the numbers at a glance
        _stats.Children.Clear();
        void Stat(string label, string value, string icon, IBrush? accent = null) =>
            _stats.Children.Add(Ui.Stat(label, value, icon, accent).Also(c => c.Margin = new Thickness(0, 0, 12, 0)));
        var problems = items.Count(Problem);
        Stat(what == "mod" ? "Mods" : "Plugins", items.Count(x => !x.B("auto")).ToString(), "puzzle", Ui.Brand);
        Stat("Dependencies", items.Count(x => x.B("auto")).ToString(), "layers");
        Stat("Need attention", problems.ToString(), "alert", problems > 0 ? Ui.Red : null);
        Stat("Updates", _updates.Count > 0 ? _updates.Count.ToString() : "—", "download", _updates.Count > 0 ? Ui.Orange : null);
        Stat("On disk", Size(items.Sum(x => x.L("size"))), "drive");

        _chips.Children.Clear();
        foreach (var (key, label) in new[] { ("all", "All"), ("attention", "Needs attention"), ("yours", "Installed by you"),
                                              ("deps", "Dependencies"), ("disabled", "Disabled") })
        {
            var k = key;
            _chips.Children.Add(Ui.Chip(label, _filter == key, () =>
            {
                _filter = k;
                Render();
            }, items.Count(x => Pass(x, k))));
        }

        var q = (_find.Text ?? "").Trim();
        var shown = items.Where(x => Pass(x, _filter) && (q.Length == 0 ||
            (x.S("name") + " " + x.S("file") + " " + x.S("source")).Contains(q, StringComparison.OrdinalIgnoreCase)));
        shown = S.Prefs.ContentSort switch
        {
            "recent" => shown.OrderByDescending(x => x.L("installed_at")),
            "size" => shown.OrderByDescending(x => x.L("size")),
            "source" => shown.OrderBy(x => x.S("source").Length == 0 ? "~" : x.S("source")).ThenBy(x => x.S("name"), StringComparer.OrdinalIgnoreCase),
            _ => shown.OrderBy(x => x.S("name"), StringComparer.OrdinalIgnoreCase),
        };
        var list = shown.ToList();

        _list.Children.Clear();
        if (items.Count == 0)
        {
            _list.Children.Add(Ui.Empty("No " + what + "s yet. Find some under Browse.", "puzzle"));
            return;
        }
        if (list.Count == 0)
        {
            _list.Children.Add(Ui.Empty("Nothing matches that.", "search"));
            return;
        }
        if (_filter != "all")
        {
            foreach (var x in list) _list.Children.Add(InstalledRow(x));
            return;
        }
        // Everything, in the order it deserves attention.
        foreach (var (title, pick) in new (string, Func<JsonNode, bool>)[]
                 {
                     ("Needs attention", x => Problem(x) || _updates.ContainsKey(x.S("file"))),
                     ("Installed by you", x => !x.B("auto") && x.B("enabled")),
                     ("Dependencies", x => x.B("auto") && x.B("enabled")),
                     ("Disabled", x => !x.B("enabled")),
                 })
        {
            var part = list.Where(x => pick(x) && (title == "Needs attention" || !(Problem(x) || _updates.ContainsKey(x.S("file"))))).ToList();
            if (part.Count == 0) continue;
            _list.Children.Add(Ui.H(8, Ui.Eyebrow(title), Ui.Pill(part.Count.ToString(), title == "Needs attention" ? "bad" : "", false))
                .Also(h => h.Margin = new Thickness(2, _list.Children.Count == 0 ? 0 : 10, 0, 2)));
            foreach (var x in part) _list.Children.Add(InstalledRow(x));
        }
    }

    static string Size(long bytes) =>
        bytes >= 1 << 30 ? (bytes / (double)(1 << 30)).ToString("0.0") + " GB"
        : bytes >= 1 << 20 ? (bytes / (double)(1 << 20)).ToString("0.0") + " MB"
        : (bytes / 1024.0).ToString("0") + " KB";

    Control RowIcon(JsonNode x, bool on)
    {
        Border b;
        if (x.S("icon").Length > 0) b = Images.Project(x.S("icon"), 40, 10);
        else if (x.B("jar_icon")) b = Images.FromEngine("/api/content/icon?sid=" + Api.Q(Sid) + "&file=" + Api.Q(x.S("file")), 40, 10);
        else b = Ui.Rounded(Icons.Make("puzzle", 18, on ? Ui.Brand : Ui.Secondary), 40, 10);
        if (!on) b.Opacity = 0.55;
        return b;
    }

    Control InstalledRow(JsonNode x)
    {
        var file = x.S("file");
        var on = x.B("enabled");
        var sub = new List<string>();
        if (x.S("version").Length > 0) sub.Add(x.S("version"));
        sub.Add(x.S("source").Length > 0 ? ContentBrowser.SourceName(x.S("source")) : "added by hand");
        sub.Add(file);
        var info = Ui.TitleSub(x.S("name", file), string.Join("  ·  ", sub),
            on ? null : Ui.Pill("disabled", "", false),
            x.B("auto") ? Ui.Pill("dependency", "info", false) : null,
            x.A("problems").Count > 0 ? Ui.Pill("problem", "bad", false) : null,
            _updates.ContainsKey(file) ? Ui.Pill("update", "warn", false) : null);
        foreach (var pr in x.Strings("problems")) info.Children.Add(Ui.T(pr, null, 12.5, Ui.Red, wrap: true));
        foreach (var n in x.Strings("notes")) info.Children.Add(Ui.T(n, null, 12.5, Ui.Orange, wrap: true));
        var needed = x.Strings("required_by");
        if (needed.Count > 0) info.Children.Add(Ui.Hint("Needed by " + string.Join(", ", needed)));

        Button? upd = null;
        if (_updates.TryGetValue(file, out var u))
            upd = Ui.Btn("Update to " + u.S("latest"), () => _ = Update(file), "download", "brand sm");
        var row = Ui.Row(
            Ui.Lead(RowIcon(x, on), info),
            upd,
            Ui.Switch(null, on, v => { _ = Toggle(file); }).Also(t => ToolTip.SetTip(t, on ? "Disable" : "Enable")),
            x.S("url").Length > 0 ? Ui.IconBtn("external", "Open its page", () => MainWindow.OpenUrl(x.S("url")), "ghost", 32) : null,
            Ui.IconBtn("trash", "Remove", () => _ = Remove(x), "soft-red", 32));
        row.Opacity = on ? 1 : 0.7;
        return row;
    }

    async Task Install(JsonNode hit)
    {
        var item = await ContentBrowser.ChooseVersion("sid=" + Api.Q(Sid), hit, Sid, null, null);
        if (item == null) return;
        Toasts.Show("Installing " + item.S("name") + " - the console has the detail.");
        var j = await Ops.Act("/api/content/install", new JsonObject { ["sid"] = Sid, ["items"] = new JsonArray(item) });
        if (j == null) return;
        foreach (var w in j.N("report").Strings("warnings")) Toasts.Show(w);
        await LoadInstalled();
    }

    async Task CheckUpdates()
    {
        _checkBtn.IsEnabled = false;
        _count.Text = "checking for updates…";
        try
        {
            var j = await Api.Get("/api/content/updates?sid=" + Api.Q(Sid));
            _updates.Clear();
            foreach (var u in j.A("items").Items()) _updates[u.S("file")] = u;
            foreach (var e in j.Strings("errors")) Toasts.Bad(e);
            if (_updates.Count == 0) Toasts.Good("Everything is up to date.");
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
        finally
        {
            _checkBtn.IsEnabled = true;
        }
        await LoadInstalled();
    }

    async Task Update(string file)
    {
        Toasts.Show("Updating - the console has the detail.");
        var j = await Ops.Act("/api/content/update", new JsonObject { ["sid"] = Sid, ["files"] = new JsonArray(file) });
        if (j != null) _updates.Remove(file);
        await LoadInstalled();
    }

    async Task UpdateAll()
    {
        Toasts.Show("Updating " + Ui.Plural(_updates.Count, "file") + " - the console has the detail.");
        var j = await Ops.Act("/api/content/update", new JsonObject { ["sid"] = Sid });
        if (j != null) _updates.Clear();
        await LoadInstalled();
    }

    async Task Toggle(string file)
    {
        var j = await Ops.Act("/api/content/toggle", new { sid = Sid, file }, null);
        if (j != null) Toasts.Good(j.B("enabled") ? "Enabled." : "Disabled." + (j.B("restart") ? " Restart the server to apply." : ""));
        await LoadInstalled();
    }

    async Task Remove(JsonNode x)
    {
        var needed = x.Strings("required_by");
        var text = "This deletes " + x.S("file") + " from " + _profile.S("dir") + "/. Its config folder is kept.";
        if (needed.Count > 0) text = string.Join(", ", needed) + (needed.Count == 1 ? " needs" : " need") + " it and will stop working. " + text;
        if (!await Dialogs.Confirm("Remove " + x.S("name", x.S("file")) + "?", text, "Remove")) return;
        await Ops.Act("/api/content/remove", new { sid = Sid, file = x.S("file") });
        _updates.Remove(x.S("file"));
        await LoadInstalled();
    }
}
