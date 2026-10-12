using System.Globalization;
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
/// Search Modrinth, Hangar and SpigotMC for what one server can load. Used by
/// the Mods &amp; plugins tab (pick = install now) and by the Create server
/// wizard (pick = add to the list the new server gets). The engine does the
/// filtering: it only offers the sources and loaders that fit the server, so
/// a Fabric server is never shown a Bukkit plugin.
/// </summary>
public sealed class ContentBrowser
{
    readonly Func<string> _target;              // "sid=…" or "software=…&mc=…"
    readonly Func<JsonNode, Task> _pick;
    readonly string _pickLabel, _pickIcon;
    readonly Func<JsonNode, bool>? _picked;

    public Control View { get; }

    JsonNode? _profile;
    string _source = "modrinth";
    int _offset;
    int _total;
    readonly ContentControl _sources = new();
    readonly WrapPanel _cats = new();
    string _category = "";
    readonly Dictionary<string, List<JsonNode>> _catCache = new();
    readonly TextBox _q;
    readonly ComboBox _sort;
    readonly ToggleButton _all;
    readonly StackPanel _results = Ui.V(8);
    readonly TextBlock _status = Ui.Hint("");
    readonly Button _prev, _next;
    int _seq;

    public ContentBrowser(Func<string> target, Func<JsonNode, Task> pick, string pickLabel = "Install",
                          string pickIcon = "download", Func<JsonNode, bool>? picked = null)
    {
        _target = target;
        _pick = pick;
        _pickLabel = pickLabel;
        _pickIcon = pickIcon;
        _picked = picked;

        _q = Ui.Input("", "Search by name - or leave empty to browse the most popular").OnEnter(() => _ = Search(0));
        _q.InnerLeftContent = Icons.Make("search", 16, Ui.Secondary).Also(i => i.Margin = new Thickness(12, 0, 0, 0));
        _sort = Ui.Select(new[] { ("relevance", "Relevance"), ("downloads", "Downloads"), ("updated", "Recently updated"), ("newest", "Newest") },
            "relevance", v => { _ = Search(0); }, 170);
        _all = Ui.Switch("show other versions too", false, v => { _ = Search(0); });
        ToolTip.SetTip(_all, "Off: only what has a build for this exact Minecraft version. On: everything for this loader, marked when it does not fit.");

        var qRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto,Auto") };
        qRow.Children.Add(_q);
        _sort.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(_sort, 1);
        qRow.Children.Add(_sort);
        var go = Ui.Btn("Search", () => _ = Search(0), "search", "brand");
        go.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(go, 2);
        qRow.Children.Add(go);

        _prev = Ui.Btn("Previous", () => _ = Search(Math.Max(0, _offset - 20)), "arrow-left", "sm");
        _next = Ui.Btn("Next", () => _ = Search(_offset + 20), "chevron-right", "sm");
        _prev.IsEnabled = _next.IsEnabled = false;

        View = Ui.V(14,
            Ui.Split(_sources, _all),
            qRow,
            _cats,
            _status,
            _results,
            Ui.Split(new Border(), Ui.H(8, _prev, _next)));
    }

    /// <summary>The server changed (or the wizard changed software/version): rebuild and search again.</summary>
    public async Task SetProfile(JsonNode profile)
    {
        _profile = profile;
        var sources = profile.Strings("sources");
        if (!sources.Contains(_source)) _source = sources.FirstOrDefault() ?? "modrinth";
        if (sources.Count == 0)
        {
            _sources.Content = null;
            _results.Children.Clear();
            _results.Children.Add(Ui.Empty(profile.S("reason", "This server takes no mods or plugins."), "puzzle"));
            _status.Text = "";
            _prev.IsEnabled = _next.IsEnabled = false;
            return;
        }
        var (bar, _) = Ui.NavTabs(sources.Select(s => (s, SourceName(s), (string?)SourceIcon(s))), _source, k =>
        {
            _source = k;
            _category = "";
            _ = LoadCategories();
            _ = Search(0);
        });
        _sources.Content = bar;
        _ = LoadCategories();
        await Search(0);
    }

    /// <summary>Category chips for the source on screen (Modrinth and Hangar have them).</summary>
    async Task LoadCategories()
    {
        var src = _source;
        if (!_catCache.TryGetValue(src, out var cats))
        {
            try
            {
                cats = (await Api.Get("/api/content/categories?" + _target() + "&source=" + Api.Q(src))).A("items").Items().ToList();
            }
            catch (Exception)
            {
                cats = new List<JsonNode>();
            }
            _catCache[src] = cats;
        }
        if (src != _source) return;
        RenderCategories(cats);
    }

    void RenderCategories(List<JsonNode> cats)
    {
        _cats.Children.Clear();
        _cats.IsVisible = cats.Count > 0;
        if (cats.Count == 0) return;
        _cats.Children.Add(Ui.Chip("All", _category.Length == 0, () => PickCategory("", cats)));
        foreach (var c in cats)
        {
            var id = c.S("id");
            _cats.Children.Add(Ui.Chip(c.S("name"), _category == id, () => PickCategory(id, cats)));
        }
    }

    void PickCategory(string id, List<JsonNode> cats)
    {
        _category = _category == id ? "" : id;
        RenderCategories(cats);
        _ = Search(0);
    }

    public static string SourceName(string s) => s switch
    {
        "modrinth" => "Modrinth",
        "hangar" => "Hangar",
        "spigot" => "SpigotMC",
        _ => s,
    };

    static string SourceIcon(string s) => s switch
    {
        "modrinth" => "box",
        "hangar" => "package",
        _ => "puzzle",
    };

    public static string Count(double n) =>
        n >= 1_000_000 ? (n / 1_000_000).ToString("0.#", CultureInfo.InvariantCulture) + "M"
        : n >= 1_000 ? (n / 1_000).ToString("0.#", CultureInfo.InvariantCulture) + "k"
        : n.ToString("0", CultureInfo.InvariantCulture);

    async Task Search(int offset)
    {
        if (_profile == null) return;
        var seq = ++_seq;
        _offset = offset;
        _status.Text = "searching " + SourceName(_source) + "…";
        _prev.IsEnabled = _next.IsEnabled = false;
        JsonNode j;
        try
        {
            j = await Api.Get("/api/content/search?" + _target() + "&source=" + Api.Q(_source) + "&q=" + Api.Q(_q.Text) +
                              "&offset=" + offset + "&sort=" + Api.Q(Ui.Val(_sort) ?? "relevance") + (_all.IsChecked == true ? "&all=1" : "") +
                              (_category.Length > 0 ? "&category=" + Api.Q(_category) : ""));
        }
        catch (Exception e)
        {
            if (seq != _seq) return;
            _results.Children.Clear();
            _results.Children.Add(Ui.Empty(e.Message, "alert"));
            _status.Text = "";
            return;
        }
        if (seq != _seq) return;
        _total = j.I("total");
        var items = j.A("items").Items().ToList();
        _results.Children.Clear();
        var p = j.N("profile") ?? _profile;
        var what = p.S("kind") == "mod" ? "mods" : "plugins";
        _status.Text = items.Count == 0 ? "" :
            Count(_total) + " " + what + " for " + p.S("label") + (p.S("mc").Length > 0 ? " " + p.S("mc") : "") +
            " on " + SourceName(_source) + "  ·  showing " + (offset + 1) + "–" + (offset + items.Count);
        if (items.Count == 0)
            _results.Children.Add(Ui.Empty(_q.Text?.Length > 0
                ? "Nothing on " + SourceName(_source) + " matches that for " + p.S("label") + " " + p.S("mc") + "."
                : "Nothing found.", "search"));
        foreach (var h in items) _results.Children.Add(Hit(h).Also(r => r.Tag = h));
        _prev.IsEnabled = offset > 0;
        _next.IsEnabled = offset + items.Count < _total;
    }

    Control Hit(JsonNode h)
    {
        var compat = h.N("compatible");
        var fits = compat is JsonValue cv && cv.TryGetValue(out bool b) ? (bool?)b : null;
        var tags = new WrapPanel();
        void Tag(Control c)
        {
            c.Margin = new Thickness(0, 4, 6, 0);
            tags.Children.Add(c);
        }
        Tag(Ui.Tag(Count(h.D("downloads")), "download"));
        foreach (var c in h.Strings("categories").Take(3)) Tag(Ui.Tag(c));
        if (fits == true) Tag(Ui.Pill("fits", "on", false));
        else if (fits == false) Tag(Ui.Pill("does not fit", "bad", false));
        else Tag(Ui.Pill("unverified", "warn", false));

        var title = Ui.H(8, Ui.T(h.S("name"), "h3"), h.S("author").Length > 0 ? Ui.T("by " + h.S("author"), null, 12.5, Ui.Secondary) : null);
        var desc = Ui.T(h.S("description"), null, 12.5, Ui.Text, wrap: true);
        desc.MaxLines = 2;
        desc.TextTrimming = TextTrimming.CharacterEllipsis;
        var body = Ui.V(3, title, desc, tags);
        if (h.S("note").Length > 0)
            body.Children.Add(Ui.T(h.S("note"), null, 12, fits == false || !h.B("installable") ? Ui.Red : Ui.Orange, wrap: true));

        var already = _picked?.Invoke(h) == true;
        Border row = null!;
        var pickBtn = Ui.Btn(already ? "Added" : _pickLabel, () => _ = DoPick(h, row), already ? "check" : _pickIcon, already ? "sm" : "brand sm");
        pickBtn.IsEnabled = h.B("installable") && !already;
        if (!h.B("installable")) ToolTip.SetTip(pickBtn, h.S("note"));
        row = Ui.Row(Ui.Lead(Images.Project(h.S("icon"), 48), body),
            pickBtn,
            h.S("url").Length > 0 ? Ui.IconBtn("external", "Open its page", () => MainWindow.OpenUrl(h.S("url")), "ghost", 32) : null);
        if (fits == false) row.Opacity = 0.8;
        return row;
    }

    async Task DoPick(JsonNode h, Border row)
    {
        await _pick(h);
        // In the wizard "Add" becomes "Added" without searching again.
        if (_picked == null) return;
        var i = _results.Children.IndexOf(row);
        if (i >= 0) _results.Children[i] = Hit(h).Also(r => r.Tag = h);
    }

    /// <summary>Redraw the current results, e.g. after the wizard's list changed.</summary>
    public void Rerender()
    {
        for (var i = 0; i < _results.Children.Count; i++)
            if (_results.Children[i].Tag is JsonNode h)
                _results.Children[i] = Hit(h).Also(r => r.Tag = h);
    }

    // ================================================================ install dialog

    /// <summary>
    /// Pick a version, see exactly what will be installed (dependencies
    /// included) and what will not, then confirm. Returns the item to install,
    /// or null when cancelled.
    /// </summary>
    public static async Task<JsonObject?> ChooseVersion(string target, JsonNode hit, string? sid, string? software, string? mc)
    {
        JsonNode j;
        try
        {
            j = await Api.Get("/api/content/project?" + target + "&source=" + Api.Q(hit.S("source")) + "&id=" + Api.Q(hit.S("id")));
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
            return null;
        }
        var p = j.N("project");
        var versions = p.A("versions").Items().ToList();
        if (versions.Count == 0)
        {
            await Dialogs.Info(p.S("name"), Ui.T("No versions are published.", null, 14, Ui.Text, wrap: true));
            return null;
        }
        var first = versions.FirstOrDefault(v => v.B("compatible")) ?? versions[0];
        var opts = versions.Select(v => (v.S("id"),
            v.S("number") + (v.S("channel") != "release" ? "  (" + v.S("channel") + ")" : "") +
            (v.B("compatible") ? "" : "  - does not fit"))).ToList();
        var planOut = Ui.V(8);
        string chosen = first.S("id");
        var sel = Ui.Select(opts, chosen, v =>
        {
            chosen = v;
            _ = Preview();
        });
        sel.HorizontalAlignment = HorizontalAlignment.Stretch;

        async Task Preview()
        {
            planOut.Children.Clear();
            planOut.Children.Add(Ui.Hint("working out what it needs…"));
            var item = new JsonObject { ["source"] = hit.S("source"), ["project_id"] = p.S("id"), ["version_id"] = chosen };
            JsonNode pl;
            try
            {
                var body = new JsonObject { ["items"] = new JsonArray(item) };
                if (sid != null) body["sid"] = sid;
                if (software != null) { body["software"] = software; body["mc"] = mc; }
                pl = await Api.Post("/api/content/plan", body);
            }
            catch (Exception e)
            {
                planOut.Children.Clear();
                planOut.Children.Add(Ui.T(e.Message, null, 13, Ui.Red, wrap: true));
                return;
            }
            planOut.Children.Clear();
            RenderPlan(planOut, pl);
        }

        var facts = new WrapPanel();
        void Fact(Control c)
        {
            c.Margin = new Thickness(0, 4, 6, 0);
            facts.Children.Add(c);
        }
        Fact(Ui.Tag(Count(p.D("downloads")) + " downloads", "download"));
        foreach (var c in p.Strings("categories").Take(4)) Fact(Ui.Tag(c));
        if (p.S("license").Length > 0) Fact(Ui.Tag(p.S("license"), "file-text"));
        var head = Ui.Lead(Images.Project(p.S("icon"), 64, 16),
            Ui.V(3, Ui.T(p.S("name"), "h3"), Ui.T(p.S("description"), null, 12.5, Ui.Secondary, wrap: true), facts));
        var body = Ui.V(14, head);
        // What it looks like: the project's own screenshots, featured first.
        var shots = p.A("gallery").Items().Take(3).ToList();
        if (shots.Count > 0)
        {
            var row = new UniformGrid { Columns = 3 };
            foreach (var g in shots)
            {
                var url = g.S("url");
                var thumb = Images.Shot(url, 180, 101);
                thumb.Margin = new Thickness(0, 0, 8, 0);
                thumb.Cursor = new Avalonia.Input.Cursor(Avalonia.Input.StandardCursorType.Hand);
                thumb.PointerPressed += (_, _) => MainWindow.OpenUrl(url);
                if (g.S("title").Length > 0) ToolTip.SetTip(thumb, g.S("title"));
                row.Children.Add(thumb);
            }
            body.Children.Add(row);
        }
        body.Children.Add(Ui.Field("Version", sel));
        body.Children.Add(planOut);
        if (hit.S("source") == "spigot")
            body.Children.Add(Ui.Hint(p.S("body")));
        _ = Preview();
        var r = await Dialogs.Show("Install " + p.S("name"), body, 640, ("Cancel", "", "ghost"), ("Install", "ok", "brand"));
        if (r != "ok") return null;
        return new JsonObject
        {
            ["source"] = hit.S("source"), ["project_id"] = p.S("id"), ["version_id"] = chosen, ["name"] = p.S("name"),
        };
    }

    /// <summary>"Will install / needs / will not install" - shared by the dialog and the wizard.</summary>
    public static void RenderPlan(StackPanel into, JsonNode pl)
    {
        foreach (var it in pl.A("install").Items())
        {
            into.Children.Add(Ui.Row(Ui.Lead(Ui.Rounded(Icons.Make(it.B("auto") ? "layers" : "download", 16, it.B("auto") ? Ui.Blue : Ui.Brand), 32, 9),
                Ui.TitleSub(it.S("name") + " " + it.S("version"),
                    it.S("reason") == "requested" ? "from " + SourceName(it.S("source")) : it.S("reason") + " - installed automatically"))));
        }
        foreach (var b in pl.A("blocked").Items())
            into.Children.Add(Ui.Row(Ui.Lead(Ui.Rounded(Icons.Make("circle-x", 16, Ui.Red), 32, 9),
                Ui.TitleSub(b.S("name") + " will not be installed", b.S("reason")))));
        foreach (var w in pl.Strings("warnings"))
            into.Children.Add(Ui.T(w, null, 12.5, Ui.Orange, wrap: true));
        var opt = pl.A("optional").Items().ToList();
        if (opt.Count > 0)
            into.Children.Add(Ui.Hint("Optional extras that work with it: " +
                                      string.Join(", ", opt.Select(o => o.S("name") + " (" + o.S("for") + ")"))));
        if (pl.A("install").Count == 0 && pl.A("blocked").Count == 0)
            into.Children.Add(Ui.Hint("Already installed - nothing to do."));
    }
}
