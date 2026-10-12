using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using Avalonia.VisualTree;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// Home: every server as an instance, like the Modrinth app's library, with
/// profiles that group instances and carry their Java settings. This is where
/// Lodestone opens - pick an instance and step inside it. With no servers yet
/// it is the welcome screen instead: three big ways to get the first one.
/// </summary>
public sealed class ServersPage : Page
{
    public ServersPage(MainWindow s) : base(s) { }

    public override string Key => "servers";
    public override string Title => "Instances";
    public override string Icon => "home";
    public override bool ServerScoped => false;

    ContentControl _body = null!;
    Control _listView = null!;
    Control? _welcome;
    TextBlock _hint = null!;

    // library
    string _tab = "instances";
    Action<string>? _selectTab;
    string _filter = "all";
    string _view = "grid";
    double _gridWidth = 1100;
    ContentControl _tabBody = null!;
    Control _instTools = null!;
    Border _filterBar = null!;
    WrapPanel _filters = null!;
    TextBox _search = null!;
    ComboBox _sort = null!;
    Button _gridBtn = null!, _listBtn = null!;
    TextBlock _heroStats = null!;
    Image _heroA = null!, _heroB = null!;
    int _heroIndex;
    DispatcherTimer? _heroTimer;

    List<JsonNode> _profiles = new();
    List<string> _colors = new() { "#1BD96A", "#4F9CFF", "#C78AFF", "#FFA347", "#FF496E", "#5EE6D0", "#FFD65C" };
    string _aikar = "";


    protected override Control Build()
    {
        _hint = Ui.Hint("");
        _hint.IsVisible = false;

        var (tabs, select) = Ui.NavTabs(new (string, string, string?)[]
        {
            ("instances", "Instances", "server"),
            ("profiles", "Profiles", "sliders"),
        }, _tab, k =>
        {
            _tab = k;
            Render();
        });
        _selectTab = select;

        _search = Ui.Input("", "Search instances", width: 240);
        _search.Classes.Add("search");
        _search.InnerLeftContent = Icons.Make("search", 15, Ui.Secondary).Also(i => i.Margin = new Thickness(10, 0, 0, 0));
        _search.TextChanged += (_, _) => RenderInstances();
        _sort = Ui.Select(new[]
        {
            ("status", "Running first"), ("name", "Name"), ("recent", "Recently started"), ("profile", "Group by profile"),
        }, "status", _ => RenderInstances(), 180);
        _gridBtn = Ui.IconBtn("box", "Grid", () => SetView("grid"), "ghost", 34);
        _listBtn = Ui.IconBtn("list", "List", () => SetView("list"), "ghost", 34);
        _instTools = Ui.H(8, _search, _sort, _gridBtn, _listBtn);

        _filters = new WrapPanel();
        _filterBar = new Border
        {
            Background = Ui.Raised,
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(18),
            Padding = new Thickness(4, 4, 0, 0),
            HorizontalAlignment = HorizontalAlignment.Left,
            Child = _filters,
        };

        _tabBody = new ContentControl();
        var col = Ui.V(18, BuildHero(), _hint, Ui.Split(tabs, _instTools), _filterBar, _tabBody);
        _listView = Ui.Scroll(col, new Thickness(28, 24, 28, 28));
        _body = new ContentControl { Content = _listView };
        PaintViewButtons();
        return _body;
    }

    // ================================================================ hero

    Control BuildHero()
    {
        Image Layer(int i)
        {
            var img = new Image
            {
                Source = Art.Load(Art.Banners[i].file, 1600),
                Stretch = Stretch.UniformToFill,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            };
            RenderOptions.SetBitmapInterpolationMode(img, BitmapInterpolationMode.HighQuality);
            return img;
        }
        _heroA = Layer(0);
        _heroB = Layer(1);
        _heroB.Opacity = 0;
        foreach (var img in new[] { _heroA, _heroB })
        {
            img.RenderTransform = Anim.Tf("scale(1.0)");
            img.Transitions = new Avalonia.Animation.Transitions
            {
                new Avalonia.Animation.DoubleTransition { Property = Visual.OpacityProperty, Duration = TimeSpan.FromMilliseconds(1400) },
                new Avalonia.Animation.TransformOperationsTransition { Property = Visual.RenderTransformProperty, Duration = TimeSpan.FromSeconds(10) },
            };
        }

        var shade = new Border
        {
            Background = new LinearGradientBrush
            {
                StartPoint = new RelativePoint(0, 0.5, RelativeUnit.Relative),
                EndPoint = new RelativePoint(1, 0.5, RelativeUnit.Relative),
                GradientStops =
                {
                    new GradientStop(Color.Parse("#F2101114"), 0),
                    new GradientStop(Color.Parse("#B3101114"), 0.42),
                    new GradientStop(Color.Parse("#14101114"), 1),
                },
            },
        };
        var bottom = new Border
        {
            Background = Art.Fade(Color.Parse("#00101114"), Color.Parse("#99101114"), 0.55, true),
        };

        _heroStats = Ui.T("", null, 13.5, Ui.Hex("#D5DBE3"));
        var eyebrow = Ui.Eyebrow("Your library");
        eyebrow.Foreground = Ui.Brand;
        var actions = Ui.H(8,
            Ui.Btn("Create server", () => _ = OpenWizard(), "zap", "brand",
                "Download and install a new server - Vanilla, Paper, Purpur, Spigot, Fabric, Quilt, Forge or NeoForge"),
            Ui.Btn("Add existing", () => _ = AddExisting(), "folder-plus", "",
                "A server you already have: a folder on this PC, or one on a host over SFTP"));
        actions.Margin = new Thickness(0, 12, 0, 0);
        var pick = Ui.T("Click an instance to open it.", null, 13, Ui.Hex("#AEB6C2"));
        var text = Ui.V(4, eyebrow, Ui.T("Instances", null, 36, Ui.Contrast, FontWeight.ExtraBold), _heroStats, pick, actions);
        text.Margin = new Thickness(30, 0, 0, 0);
        text.VerticalAlignment = VerticalAlignment.Center;

        // the mascots keep watch in the corner
        var golem = Art.Figure("golem", 118);
        golem.RenderTransform = new TranslateTransform();
        golem.Classes.Add("bob");
        var cat = Art.Figure("cat", 82);
        cat.RenderTransform = new TranslateTransform();
        cat.Classes.Add("bob-slow");
        var pets = Ui.H(4, golem, cat);
        pets.HorizontalAlignment = HorizontalAlignment.Right;
        pets.VerticalAlignment = VerticalAlignment.Bottom;
        pets.Margin = new Thickness(0, 0, 22, 10);
        foreach (var p in pets.Children) p.VerticalAlignment = VerticalAlignment.Bottom;

        var pixels = new PixelField
        {
            Start = 0.55,
            Cell = 5,
            Height = 30,
            Opacity = 0.55,
            EdgeFade = true,
            VerticalAlignment = VerticalAlignment.Bottom,
        };

        var g = new Grid { Height = 232 };
        g.Children.Add(_heroA);
        g.Children.Add(_heroB);
        g.Children.Add(shade);
        g.Children.Add(bottom);
        g.Children.Add(pixels);
        g.Children.Add(pets);
        g.Children.Add(text);
        var hero = new Border
        {
            CornerRadius = new CornerRadius(20),
            ClipToBounds = true,
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1),
            Child = g,
        };
        return Anim.Enter(hero, 0, 10);
    }

    /// <summary>The hero drifts slowly and cross-fades between the scenes.</summary>
    void StartHero()
    {
        if (!Anim.Enabled || _heroTimer != null) return;
        _heroA.RenderTransform = Anim.Tf("scale(1.08)");
        _heroTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(9) };
        _heroTimer.Tick += (_, _) =>
        {
            _heroIndex = (_heroIndex + 1) % Art.Banners.Length;
            var (show, hide) = _heroA.Opacity > 0.5 ? (_heroB, _heroA) : (_heroA, _heroB);
            show.Source = Art.Load(Art.Banners[_heroIndex].file, 1600);
            show.Transitions = null;
            show.RenderTransform = Anim.Tf("scale(1.0)");
            show.Transitions = new Avalonia.Animation.Transitions
            {
                new Avalonia.Animation.DoubleTransition { Property = Visual.OpacityProperty, Duration = TimeSpan.FromMilliseconds(1400) },
                new Avalonia.Animation.TransformOperationsTransition { Property = Visual.RenderTransformProperty, Duration = TimeSpan.FromSeconds(10) },
            };
            show.Opacity = 1;
            hide.Opacity = 0;
            Dispatcher.UIThread.Post(() => show.RenderTransform = Anim.Tf("scale(1.08)"), DispatcherPriority.Background);
        };
        _heroTimer.Start();
    }

    void StopHero()
    {
        _heroTimer?.Stop();
        _heroTimer = null;
    }

    public override Task OnShow()
    {
        StartHero();
        return _wizard == null ? Load() : Task.CompletedTask;
    }

    public override void OnHide() => StopHero();

    // ================================================================ wizard

    CreateWizard? _wizard;

    /// <summary>Create server: the wizard takes the page until it is closed.</summary>
    public async Task OpenWizard(string? dir = null)
    {
        _wizard = new CreateWizard(S, CloseWizard);
        if (dir != null) _wizard.UseDir(dir);
        _body.Content = _wizard.View;
        Anim.PageIn(_wizard.View);
        await _wizard.Start();
    }

    /// <summary>For --shots.</summary>
    public async Task ShotWizard(int step, string software, string mc)
    {
        _wizard = new CreateWizard(S, CloseWizard);
        _body.Content = _wizard.View;
        await _wizard.ShotStep(step, software, mc);
    }

    void CloseWizard()
    {
        _wizard = null;
        _body.Content = _listView;
        Anim.PageIn(_listView);
        _ = Load();
    }

    /// <summary>For --shots: the welcome screen, whatever is on the shelf.</summary>
    public void ShotWelcome()
    {
        _wizard = null;
        _welcome = BuildWelcome();
        _body.Content = _welcome;
    }

    /// <summary>For --shots.</summary>
    public async Task ShowTab(string tab, string view = "grid")
    {
        _wizard = null;
        _body.Content = _listView;
        _tab = tab;
        _selectTab?.Invoke(tab);
        _view = view;
        PaintViewButtons();
        await Load();
    }

    public override void OnStatus(JsonNode s)
    {
        if (!Built) return;
        var err = s.N("transport").S("error");
        _hint.Text = err;
        _hint.IsVisible = err.Length > 0;
    }

    // ================================================================ data

    async Task Load()
    {
        try { await S.LoadShelf(); }
        catch (Exception e) { Toasts.Bad(e.Message); }
        try
        {
            var j = await Api.Get("/api/profiles");
            _profiles = j.A("items").Items().ToList();
            var cols = j.Strings("colors");
            if (cols.Count > 0) _colors = cols;
            _aikar = j.S("aikar");
        }
        catch (Exception)
        {
            _profiles = new List<JsonNode>();
        }
        var running = S.Servers.Count(x => x.B("running"));
        _heroStats.Text = Ui.Plural(S.Servers.Count, "instance") + "  ·  " + running + " running  ·  " +
                          Ui.Plural(_profiles.Count, "profile");
        if (_wizard == null)
        {
            // Nothing on the shelf yet: the welcome screen, not an empty library.
            var want = S.Servers.Count == 0 ? (_welcome ??= BuildWelcome()) : _listView;
            if (_body.Content != want)
            {
                _body.Content = want;
                Anim.PageIn(want);
            }
            if (S.Servers.Count > 0) _welcome = null;
        }
        Render();
    }

    JsonNode? Profile(string id) => id.Length == 0 ? null : _profiles.FirstOrDefault(p => p.S("id") == id);

    internal static bool HasSettings(JsonNode? p) =>
        p != null && (p.S("java").Length > 0 || p.S("min_ram").Length > 0 || p.S("max_ram").Length > 0 || p.S("extra").Length > 0);

    void Render()
    {
        var inst = _tab == "instances";
        _instTools.IsVisible = inst;
        _filterBar.IsVisible = inst;
        if (inst) RenderInstances();
        else RenderProfiles();
    }

    void SetView(string v)
    {
        _view = v;
        PaintViewButtons();
        RenderInstances();
    }

    void PaintViewButtons()
    {
        if (_gridBtn == null) return;
        _gridBtn.Classes.Set("soft", _view == "grid");
        _gridBtn.Classes.Set("ghost", _view != "grid");
        _listBtn.Classes.Set("soft", _view == "list");
        _listBtn.Classes.Set("ghost", _view != "list");
    }

    // ================================================================ instances

    void RenderFilters()
    {
        _filters.Children.Clear();
        void Chip(string key, string label, IBrush? dot, int count)
        {
            var inner = Ui.H(7);
            if (dot != null) inner.Children.Add(new Border { Width = 8, Height = 8, CornerRadius = new CornerRadius(4), Background = dot });
            inner.Children.Add(new TextBlock { Text = label, VerticalAlignment = VerticalAlignment.Center });
            inner.Children.Add(Ui.T(count.ToString(), null, 12, Ui.Secondary, FontWeight.SemiBold));
            var b = new Button { Content = inner, Margin = new Thickness(0, 0, 4, 4) };
            b.Classes.Add("tab");
            b.Classes.Set("active", _filter == key);
            b.Click += (_, _) =>
            {
                _filter = key;
                RenderInstances();
            };
            _filters.Children.Add(b);
        }
        Chip("all", "All", null, S.Servers.Count);
        Chip("running", "Running", Ui.Brand, S.Servers.Count(Up));
        Chip("local", "Local", null, S.Servers.Count(x => x.S("kind") == "local"));
        Chip("remote", "Remote", null, S.Servers.Count(x => x.S("kind") != "local"));
        foreach (var p in _profiles)
            Chip("p:" + p.S("id"), p.S("name"), Ui.Hex(p.S("color", "#1BD96A")), S.Servers.Count(x => x.S("profile") == p.S("id")));
        if (_filter.StartsWith("p:") && Profile(_filter[2..]) == null) _filter = "all";
    }

    /// <summary>Running, or a remote one connected. A local folder that is merely there is not "up".</summary>
    static bool Up(JsonNode s) => s.B("running") || (s.S("kind") != "local" && s.S("state") == "online");

    List<JsonNode> Visible()
    {
        var q = (_search.Text ?? "").Trim();
        IEnumerable<JsonNode> list = S.Servers.Where(s => _filter switch
        {
            "running" => Up(s),
            "local" => s.S("kind") == "local",
            "remote" => s.S("kind") != "local",
            var f when f.StartsWith("p:") => s.S("profile") == f[2..],
            _ => true,
        });
        if (q.Length > 0)
            list = list.Where(s =>
                (s.S("name") + " " + s.S("where") + " " + s.S("software") + " " + s.S("mc") + " " + Profile(s.S("profile")).S("name"))
                .Contains(q, StringComparison.OrdinalIgnoreCase));
        return (Ui.Val(_sort) ?? "status") switch
        {
            "name" => list.OrderBy(s => s.S("name"), StringComparer.OrdinalIgnoreCase).ToList(),
            "recent" => list.OrderByDescending(s => s.L("last_started")).ThenBy(s => s.S("name"), StringComparer.OrdinalIgnoreCase).ToList(),
            "profile" => list.OrderBy(s => Profile(s.S("profile")) == null ? 1 : 0)
                .ThenBy(s => Profile(s.S("profile")).S("name"), StringComparer.OrdinalIgnoreCase)
                .ThenBy(s => s.S("name"), StringComparer.OrdinalIgnoreCase).ToList(),
            _ => list.OrderByDescending(Up).ThenByDescending(s => s.B("active"))
                .ThenBy(s => s.S("name"), StringComparer.OrdinalIgnoreCase).ToList(),
        };
    }

    void RenderInstances()
    {
        if (!Built || _tab != "instances") return;
        RenderFilters();
        var items = Visible();
        var host = Ui.V(22);
        if (items.Count == 0)
            host.Children.Add(EmptyState("Nothing matches - try another filter or search.", "cat"));
        else if (Ui.Val(_sort) == "profile")
        {
            foreach (var grp in items.GroupBy(s => s.S("profile")))
            {
                var p = Profile(grp.Key);
                var head = Ui.H(10,
                    new Border { Width = 10, Height = 10, CornerRadius = new CornerRadius(5), Background = p == null ? Ui.Ring : Ui.Hex(p.S("color", "#1BD96A")) },
                    Ui.H2(p == null ? "No profile" : p.S("name")),
                    Ui.T(Ui.Plural(grp.Count(), "instance"), null, 13, Ui.Secondary));
                var row = Ui.Split(head, p != null && HasSettings(p)
                    ? Ui.Btn("Apply settings", () => _ = ApplyProfile(p.S("id")), "check", "sm", "Copy this profile's Java settings into its local instances")
                    : null);
                host.Children.Add(Ui.V(12, row, Collection(grp.ToList())));
            }
        }
        else host.Children.Add(Collection(items));
        _tabBody.Content = host;
    }

    Control Collection(List<JsonNode> items)
    {
        if (_view == "list")
        {
            var v = Ui.V(8);
            for (var i = 0; i < items.Count; i++) v.Children.Add(Anim.Enter(InstanceRow(items[i]), Math.Min(i, 14) * 30, 10));
            return v;
        }
        var grid = new UniformGrid { Columns = Math.Max(1, (int)(_gridWidth / 330)) };
        grid.SizeChanged += (_, e) =>
        {
            _gridWidth = e.NewSize.Width;
            var c = Math.Max(1, (int)(e.NewSize.Width / 330));
            if (c != grid.Columns) grid.Columns = c;
        };
        for (var i = 0; i < items.Count; i++) grid.Children.Add(Anim.Enter(InstanceCard(items[i]), Math.Min(i, 12) * 45));
        return grid;
    }

    string SubLine(JsonNode s)
    {
        var parts = new List<string>();
        var sw = SoftwareName(s.S("software"));
        if (sw.Length > 0) parts.Add((sw + " " + s.S("mc")).Trim());
        parts.Add(s.S("kind") == "local" ? "Local" : "Remote");
        if (s.S("kind") == "local" && s.I("port") > 0) parts.Add(":" + s.I("port"));
        return string.Join("  ·  ", parts);
    }

    Border StatusPill(JsonNode s)
    {
        var running = s.B("running");
        var state = running ? "running" : s.S("kind") == "local" ? "stopped" : s.S("state", "offline");
        var kind = running || state == "online" ? "on" : "";
        var p = Ui.Pill(state, kind);
        if ((running || state == "online") && p.Child is StackPanel sp && sp.Children.Count > 0 && sp.Children[0] is Border dot)
            dot.Classes.Add("pulse");
        return p;
    }

    internal static Control ProfileChip(JsonNode? p, bool onImage)
    {
        if (p == null) return new Border();
        var b = new Border
        {
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(9, 3, 10, 3),
            Background = onImage ? Ui.Hex("#B3101114") : Ui.ButtonBg,
            Child = Ui.H(6,
                new Border { Width = 7, Height = 7, CornerRadius = new CornerRadius(4), Background = Ui.Hex(p.S("color", "#1BD96A")) },
                Ui.T(p.S("name"), null, 12, Ui.Contrast, FontWeight.SemiBold)),
            VerticalAlignment = VerticalAlignment.Center,
        };
        ToolTip.SetTip(b, "Profile: " + p.S("name"));
        return b;
    }

    WrapPanel Tags(JsonNode s)
    {
        var tags = new WrapPanel();
        void Tag(Control c)
        {
            c.Margin = new Thickness(0, 0, 6, 6);
            tags.Children.Add(c);
        }
        if (s.B("active")) Tag(Ui.Tag("Last opened", "eye", Ui.Brand));
        if (s.S("max_ram").Length > 0) Tag(Ui.Tag(s.S("max_ram") + " RAM", "cpu"));
        if (s.B("rcon")) Tag(Ui.Tag("RCON", "console"));
        if (s.I("hooks") > 0) Tag(Ui.Tag(Ui.Plural(s.I("hooks"), "webhook"), "discord"));
        var via = s.N("via");
        if (via != null && via.A("installed").Count > 0) Tag(Ui.Tag("Via", "check"));
        if (s.B("offline_mode")) Tag(Ui.Tag("Offline mode", "alert", Ui.Red));
        return tags;
    }

    Control InstanceCard(JsonNode s)
    {
        var id = s.S("id");
        var local = s.S("kind") == "local";
        var running = s.B("running");
        var active = s.B("active");
        var look = s.N("look");
        var profile = Profile(s.S("profile"));

        var banner = Art.Banner(Art.BannerKey(look.S("banner"), id), 124, new CornerRadius(15, 15, 0, 0), Ui.Raised, out var bannerImg);
        var overlay = Ui.Split(ProfileChip(profile, true), StatusPill(s));
        overlay.Margin = new Thickness(12);
        overlay.VerticalAlignment = VerticalAlignment.Top;
        var top = new Grid();
        top.Children.Add(banner);
        top.Children.Add(overlay);

        var mascot = new Border
        {
            Child = Images.Icon(s.S("icon"), 64, 16),
            CornerRadius = new CornerRadius(19),
            Padding = new Thickness(3),
            Background = Ui.Raised,
            BoxShadow = BoxShadows.Parse("0 6 16 0 #66000000"),
            VerticalAlignment = VerticalAlignment.Top,
            Margin = new Thickness(0, -38, 0, 0),
        };
        var name = Ui.T(s.S("name"), null, 17, Ui.Contrast, FontWeight.Bold);
        name.TextTrimming = TextTrimming.CharacterEllipsis;
        ToolTip.SetTip(name, s.S("name"));
        var sub = Ui.T(SubLine(s), null, 12.5, Ui.Secondary);
        sub.TextTrimming = TextTrimming.CharacterEllipsis;
        var titles = Ui.V(3, name, sub);
        titles.Margin = new Thickness(12, 6, 0, 0);
        var head = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*") };
        head.Children.Add(mascot);
        Grid.SetColumn(titles, 1);
        head.Children.Add(titles);

        var left = Ui.H(6);
        if (local)
            left.Children.Add(running
                ? Ui.Btn("Stop", () => _ = Stop(id), "stop", "danger sm")
                : Ui.Btn("Play", () => _ = Start(id), "play", "brand sm", "Start the server"));
        left.Children.Add(Ui.Btn("Open", () => _ = OpenInstance(id), "chevron-right", "soft sm", "Step inside: console, files, mods and the rest"));
        var right = Ui.H(4, Ui.IconBtn("sliders", "Instance settings", () => _ = Edit(id), "ghost", 34), MoreButton(s));
        var actions = Ui.Split(left, right);

        var body = Ui.V(12, head, Tags(s), actions);
        body.Margin = new Thickness(16, 0, 16, 16);

        var col = new Grid { RowDefinitions = new RowDefinitions("Auto,*") };
        col.Children.Add(top);
        Grid.SetRow(body, 1);
        col.Children.Add(body);

        var card = new Border
        {
            Child = col, ClipToBounds = true, Margin = new Thickness(0, 0, 14, 14),
            Cursor = new Cursor(StandardCursorType.Hand),
        };
        card.Classes.Add("card");
        if (active) card.BorderBrush = Ui.Hex("#661BD96A");
        ToolTip.SetTip(card, "Open " + s.S("name") + " - right-click for more");
        card.ContextFlyout = MoreMenu(s, true);
        // one click anywhere but a button opens it
        card.Tapped += (_, e) =>
        {
            if (e.Source is Visual v && v.FindAncestorOfType<Button>() == null && v is not Button) _ = OpenInstance(id);
        };
        Anim.Lift(card, 4, active ? Ui.Brand : Ui.Ring);
        Anim.HoverTransform(card, bannerImg, "scale(1.07)");
        return card;
    }

    Control InstanceRow(JsonNode s)
    {
        var id = s.S("id");
        var local = s.S("kind") == "local";
        var running = s.B("running");
        var look = s.N("look");
        var lead = Images.Icon(s.S("icon"), 44, 12);
        var main = Ui.Lead(lead, Ui.TitleSub(s.S("name"), SubLine(s) + (s.S("where").Length > 0 ? "  ·  " + s.S("where") : ""),
            StatusPill(s), ProfileChip(Profile(s.S("profile")), false)));
        var acts = new List<Control?>();
        if (local)
            acts.Add(running ? Ui.Btn("Stop", () => _ = Stop(id), "stop", "danger sm") : Ui.Btn("Play", () => _ = Start(id), "play", "brand sm"));
        acts.Add(Ui.Btn("Open", () => _ = OpenInstance(id), "chevron-right", "soft sm"));
        acts.Add(Ui.IconBtn("sliders", "Instance settings", () => _ = Edit(id), "ghost", 32));
        acts.Add(MoreButton(s));
        var row = Ui.Row(main, acts.ToArray());
        if (s.B("active")) row.Classes.Add("active");
        row.Cursor = new Cursor(StandardCursorType.Hand);
        row.ContextFlyout = MoreMenu(s, true);
        row.Tapped += (_, e) =>
        {
            if (e.Source is Visual v && v.FindAncestorOfType<Button>() == null && v is not Button) _ = OpenInstance(id);
        };
        Anim.Lift(row, 2);
        return row;
    }

    Button MoreButton(JsonNode s)
    {
        var b = Ui.IconBtn("more", "More", () => { }, "ghost", 34);
        b.Flyout = MoreMenu(s, false);
        return b;
    }

    /// <summary>Everything you can do to an instance - the ⋯ button, and a right-click anywhere on it.</summary>
    MenuFlyout MoreMenu(JsonNode s, bool withPower)
    {
        var id = s.S("id");
        var local = s.S("kind") == "local";
        var via = s.N("via");
        var menu = new MenuFlyout();
        if (withPower && local)
        {
            if (s.B("running")) AddItem(menu, "Stop", "stop", () => _ = Stop(id), Ui.Red);
            else AddItem(menu, "Play", "play", () => _ = Start(id));
        }
        AddItem(menu, "Open", "chevron-right", () => _ = OpenInstance(id));
        AddItem(menu, "Instance settings", "sliders", () => _ = Edit(id));
        AddItem(menu, "Change icon…", "image", () => _ = ChangeIcon(s));
        AddItem(menu, "Duplicate", "copy", () => _ = Dup(id));
        if (local)
        {
            AddItem(menu, via != null && via.A("installed").Count > 0 ? "Update Via" : "Install Via", "download", () => _ = InstallVia(id));
            if (via.B("preset_ok")) AddItem(menu, "Install pack", "package", () => _ = InstallPack(id));
        }
        var prof = new MenuItem { Header = "Profile", Icon = Icons.Make("sliders", 16, Ui.Secondary) };
        var none = new MenuItem { Header = s.S("profile").Length == 0 ? "✓  No profile" : "No profile" };
        none.Click += (_, _) => _ = SetProfile(s, "");
        prof.Items.Add(none);
        foreach (var p in _profiles)
        {
            var pid = p.S("id");
            var mi = new MenuItem
            {
                Header = (s.S("profile") == pid ? "✓  " : "") + p.S("name"),
                Icon = new Border { Width = 9, Height = 9, CornerRadius = new CornerRadius(5), Background = Ui.Hex(p.S("color", "#1BD96A")) },
            };
            mi.Click += (_, _) => _ = SetProfile(s, pid);
            prof.Items.Add(mi);
        }
        var newProf = new MenuItem { Header = "New profile…", Icon = Icons.Make("plus", 16, Ui.Secondary) };
        newProf.Click += (_, _) => _ = EditProfile(Preset("Blank"));
        prof.Items.Add(new Separator());
        prof.Items.Add(newProf);
        menu.Items.Add(prof);
        menu.Items.Add(new Separator());
        AddItem(menu, "Remove from Lodestone", "trash", () => Del(id, s.S("name")), Ui.Red);
        return menu;
    }

    static void AddItem(MenuFlyout menu, string header, string icon, Action a, IBrush? fg = null)
    {
        var mi = new MenuItem { Header = header, Icon = Icons.Make(icon, 16, fg ?? Ui.Secondary) };
        if (fg != null) mi.Foreground = fg;
        mi.Click += (_, _) => a();
        menu.Items.Add(mi);
    }

    Control EmptyState(string text, string mascot)
    {
        var fig = Art.Figure(mascot, mascot == "cat" ? 90 : 120);
        fig.HorizontalAlignment = HorizontalAlignment.Center;
        fig.RenderTransform = new TranslateTransform();
        fig.Classes.Add("bob-slow");
        var t = Ui.T(text, null, 14, Ui.Secondary, wrap: true);
        t.TextAlignment = TextAlignment.Center;
        t.MaxWidth = 460;
        t.HorizontalAlignment = HorizontalAlignment.Center;
        var v = Ui.V(14, fig, t);
        v.Margin = new Thickness(0, 26);
        v.HorizontalAlignment = HorizontalAlignment.Center;
        return Anim.Enter(Ui.Card(v, 24));
    }

    public static string SoftwareName(string sw) => sw switch
    {
        "neoforge" => "NeoForge",
        "" => "",
        _ => char.ToUpperInvariant(sw[0]) + sw[1..],
    };

    Task OpenInstance(string id) => S.OpenInstance(id, "console");

    /// <summary>Its setup lives inside it now, as the Instance settings tab.</summary>
    Task Edit(string id) => S.OpenInstance(id, "instance");

    /// <summary>Pick a Minecraft item or block for it, straight from the library.</summary>
    async Task ChangeIcon(JsonNode s)
    {
        var look = s.N("look");
        var pick = await IconPicker.Pick(look.S("icon"), s.S("icon_auto"));
        if (pick == null) return;
        var body = new JsonObject
        {
            ["id"] = s.S("id"),
            ["look"] = new JsonObject { ["icon"] = pick, ["banner"] = look.S("banner") },
        };
        if (await Ops.Act("/api/servers/look", body, null) == null) return;
        await Load();
    }

    async Task Start(string id)
    {
        if (await Ops.Act("/api/start", new { sid = id }, "Starting.") == null) return;
        await Task.Delay(900);
        await Load();
    }

    async Task Stop(string id)
    {
        if (await Ops.Act("/api/stop", new { sid = id }, "Stopping.") == null) return;
        await Task.Delay(1500);
        await Load();
    }

    async Task SetProfile(JsonNode s, string pid)
    {
        var sid = s.S("id");
        if (await Ops.Act("/api/servers/look", new JsonObject { ["id"] = sid, ["profile"] = pid }, null) == null) return;
        var p = Profile(pid);
        if (p != null && s.S("kind") == "local" && HasSettings(p) &&
            await Dialogs.Confirm("Use " + p.S("name") + "'s Java settings?", ProfileSummary(p) +
                "\n\nThey are written into " + s.S("name") + "'s Local server settings. You can change them there afterwards.", "Apply", "brand"))
            await Ops.Act("/api/profiles/apply", new { id = pid, sid }, s.B("running") ? "Applied - restart the server to use them." : "Applied.");
        else
            Toasts.Good(p == null ? "Removed from its profile." : "Added to " + p.S("name") + ".");
        await Load();
    }

    async Task InstallVia(string id)
    {
        var v = S.Servers.FirstOrDefault(x => x.S("id") == id).N("via");
        if (v.IsFalse("available") || v.IsFalse("compatible"))
        {
            var text = v.S("reason", "This server does not load Bukkit plugins.") +
                       (v.S("flavour").Length > 0 ? "\n\nDetected as " + v.S("flavour") + (v.S("how").Length > 0 ? " - " + v.S("how") : "") + "." : "") +
                       "\n\nVia runs on Bukkit servers (Paper, Spigot, Purpur, CraftBukkit and their forks) and on proxies " +
                       "(Velocity, Waterfall, BungeeCord). Modded and vanilla servers have no plugin loader for it.";
            await Dialogs.Info("ViaVersion cannot be installed here", Ui.T(text, null, 14, Ui.Text, wrap: true));
            return;
        }
        Toasts.Show("Fetching ViaVersion, ViaBackwards and ViaRewind - the console has the detail.");
        var j = await Ops.Act("/api/plugins/via", new { sid = id }, null);
        if (j == null) return;
        var r = j.N("report");
        Toasts.Good(r.S("installed", "0") + " installed, " + r.S("updated", "0") + " updated, " + r.S("skipped", "0") + " already current");
        await Load();
    }

    async Task InstallPack(string id)
    {
        var v = S.Servers.FirstOrDefault(x => x.S("id") == id).N("via");
        if (!v.B("preset_ok"))
        {
            await Dialogs.Info("Nothing to install here",
                Ui.T(v.S("preset_reason", v.S("reason", "This server takes neither plugins nor mods.")), null, 14, Ui.Text, wrap: true));
            return;
        }
        Toasts.Show("Installing " + v.A("preset").Count + " packages into " + v.S("target_dir", "plugins") + "/ - the console has the detail.");
        var j = await Ops.Act("/api/plugins/pack", new { sid = id }, null);
        if (j == null) return;
        var r = j.N("report");
        Toasts.Good(r.S("installed", "0") + " installed, " + r.S("updated", "0") + " updated, " + r.S("skipped", "0") + " skipped");
        await Load();
    }

    async Task Import()
    {
        var j = await Ops.Act("/api/servers/import", null, null);
        if (j != null) Toasts.Show(j.S("msg"), j.A("added").Count > 0 ? "good" : "");
        await Load();
    }


    async Task Dup(string id)
    {
        await Ops.Act("/api/servers/duplicate", new { id }, "Duplicated.");
        await Load();
    }

    async void Del(string id, string name)
    {
        if (!await Dialogs.Confirm("Remove " + name + "?", "The server files are left alone - this only removes the entry here.", "Remove"))
            return;
        await Ops.Act("/api/servers/delete", new { id }, "Removed.");
        await Load();
    }

    // ================================================================ welcome

    /// <summary>
    /// The first thing someone new sees: no empty list, just the three ways to
    /// get a server, each one click, and the one choice worth making up front.
    /// </summary>
    Control BuildWelcome()
    {
        // a scene behind a big hello, the mascots in the corner as on the library
        var bg = new Image
        {
            Source = Art.Load(Art.Banners[0].file, 1600),
            Stretch = Stretch.UniformToFill,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        };
        RenderOptions.SetBitmapInterpolationMode(bg, BitmapInterpolationMode.HighQuality);
        var shade = new Border
        {
            Background = new LinearGradientBrush
            {
                StartPoint = new RelativePoint(0, 0.5, RelativeUnit.Relative),
                EndPoint = new RelativePoint(1, 0.5, RelativeUnit.Relative),
                GradientStops =
                {
                    new GradientStop(Color.Parse("#F2101114"), 0),
                    new GradientStop(Color.Parse("#B3101114"), 0.5),
                    new GradientStop(Color.Parse("#26101114"), 1),
                },
            },
        };
        var eyebrow = Ui.Eyebrow("Welcome");
        eyebrow.Foreground = Ui.Brand;
        var hello = Ui.V(6,
            eyebrow,
            Ui.H(14, Logo.Make(44), Ui.T("Let's get your server running", null, 32, Ui.Contrast, FontWeight.ExtraBold)
                .Also(t => t.VerticalAlignment = VerticalAlignment.Center)),
            Ui.T("Pick how you want to start. Lodestone does the fiddly parts - downloads, Java, the right settings - " +
                 "and you can add more servers whenever you like.", null, 14.5, Ui.Hex("#D5DBE3"), wrap: true).Also(t =>
            {
                t.MaxWidth = 620;
                t.HorizontalAlignment = HorizontalAlignment.Left;
            }));
        hello.Margin = new Thickness(30, 0, 200, 0);
        hello.VerticalAlignment = VerticalAlignment.Center;
        var golem = Art.Figure("golem", 118);
        golem.RenderTransform = new TranslateTransform();
        golem.Classes.Add("bob");
        var cat = Art.Figure("cat", 82);
        cat.RenderTransform = new TranslateTransform();
        cat.Classes.Add("bob-slow");
        var pets = Ui.H(4, golem, cat);
        pets.HorizontalAlignment = HorizontalAlignment.Right;
        pets.VerticalAlignment = VerticalAlignment.Bottom;
        pets.Margin = new Thickness(0, 0, 22, 10);
        foreach (var c in pets.Children) c.VerticalAlignment = VerticalAlignment.Bottom;
        var g = new Grid { Height = 220 };
        g.Children.Add(bg);
        g.Children.Add(shade);
        g.Children.Add(pets);
        g.Children.Add(hello);
        var hero = new Border { CornerRadius = new CornerRadius(20), ClipToBounds = true, BorderBrush = Ui.Divider, BorderThickness = new Thickness(1), Child = g };

        var tiles = new UniformGrid { Columns = 3 };
        tiles.SizeChanged += (_, e) =>
        {
            var c = e.NewSize.Width < 760 ? 1 : 3;
            if (c != tiles.Columns) tiles.Columns = c;
        };
        tiles.Children.Add(Anim.Enter(BigTile(Images.Icon("block/crafting_table", 64, 16), "Create a new server",
            "Pick Vanilla, Paper, Fabric, Forge or another. Lodestone downloads it, the right Java and any mods, then sets it up.",
            "Start here", () => _ = OpenWizard(), "Recommended"), 80));
        tiles.Children.Add(Anim.Enter(BigTile(Images.Icon("block/barrel", 64, 16), "Add a server I already have",
            "Point at its folder. Lodestone works out the jar, the version and the Java it needs by itself.",
            "Choose folder", () => _ = AddFolder()), 140));
        tiles.Children.Add(Anim.Enter(BigTile(Images.Icon("item/compass", 64, 16), "Connect to a remote server",
            "A server on a host you reach over SFTP - Minehut, a VPS, a friend's machine.",
            "Enter login", () => _ = AddRemote()), 200));

        var steps = Ui.H(26);
        var n = 1;
        foreach (var (title, sub) in new[]
                 {
                     ("Pick or add a server", "It appears here, in your library"),
                     ("Open it", "Console, files, mods - all its tabs"),
                     ("Press Start", "Friends join on your address"),
                 })
        {
            var badge = new Border
            {
                Width = 30, Height = 30, CornerRadius = new CornerRadius(15), Background = Ui.BrandHighlight,
                Child = Ui.T(n++.ToString(), null, 14, Ui.Brand, FontWeight.ExtraBold).Also(t =>
                {
                    t.HorizontalAlignment = HorizontalAlignment.Center;
                    t.VerticalAlignment = VerticalAlignment.Center;
                }),
            };
            steps.Children.Add(Ui.H(10, badge, Ui.V(1, Ui.T(title, null, 13.5, Ui.Contrast, FontWeight.Bold), Ui.T(sub, null, 12, Ui.Secondary))));
        }
        var how = Ui.Card(Ui.V(12, Ui.Eyebrow("How it works"), new ScrollViewer
        {
            Content = steps, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto, VerticalScrollBarVisibility = ScrollBarVisibility.Disabled,
        }), 18);

        var advanced = Ui.Switch("Show advanced tools", S.Mode == "advanced", on => _ = S.SetMode(on ? "advanced" : "simple"));
        var extras = Ui.Card(Ui.V(8,
            Ui.Split(Ui.V(3, Ui.H3("How much do you want to see?"),
                    Ui.Hint("Off keeps it simple: console, files, properties, backups, mods and settings. On adds automation, " +
                            "the AI assistant, Discord, log search and the REST API. Change it any time in Settings.")),
                advanced),
            Ui.Divider1(6),
            Ui.Split(Ui.V(3, Ui.H3("Coming from LocalHost or RemoteHost?"), Ui.Hint("Bring every server you set up there across in one go.")),
                Ui.Btn("Import them", () => _ = Import(), "import", "sm"))), 18);

        var col = Ui.V(20, Anim.Enter(hero, 0, 10), tiles, Anim.Enter(how, 260), Anim.Enter(extras, 320));
        return Ui.Scroll(col, new Thickness(28, 24, 28, 28));
    }

    static Button BigTile(Control art, string title, string sub, string go, Action click, string? badge = null)
    {
        var head = Ui.H(10, Ui.T(title, null, 18, Ui.Contrast, FontWeight.Bold).Also(t => t.TextWrapping = TextWrapping.Wrap));
        if (badge != null) head.Children.Add(Ui.Pill(badge, "on", false));
        var arrow = Ui.H(6, Ui.T(go, null, 14, Ui.Brand, FontWeight.Bold), Icons.Make("chevron-right", 16, Ui.Brand));
        art.HorizontalAlignment = HorizontalAlignment.Left;
        var body = Ui.V(12, art, head, Ui.T(sub, null, 13.5, Ui.Secondary, wrap: true), arrow);
        var b = new Button { Content = body, Margin = new Thickness(0, 0, 14, 14) };
        b.Classes.Add("bigtile");
        b.Click += (_, _) => click();
        Anim.Lift(b, 4);
        return b;
    }

    // ================================================================ adding one you have

    /// <summary>The library's "Add existing": what kind of server it is decides the next step.</summary>
    public async Task AddExisting()
    {
        Button Choice(Control art, string title, string sub, string key)
        {
            var row = Ui.Lead(art, Ui.V(3, Ui.T(title, null, 15.5, Ui.Contrast, FontWeight.Bold), Ui.T(sub, null, 13, Ui.Secondary, wrap: true)), 14);
            var b = new Button { Content = row, Margin = new Thickness(0, 0, 0, 10), MinHeight = 0 };
            b.Classes.Add("bigtile");
            b.Padding = new Thickness(16);
            b.Click += (_, _) => Dialogs.Close(key);
            return b;
        }
        var body = Ui.V(0,
            Choice(Images.Icon("block/barrel", 48, 12), "A server folder on this PC",
                "Pick the folder; the jar, version, port and Java are worked out for you.", "folder"),
            Choice(Images.Icon("item/compass", 48, 12), "A server on a host (SFTP)",
                "Minehut, a VPS or any box you log in to with SFTP.", "remote"),
            Choice(Images.Icon("item/ender_pearl", 48, 12), "From LocalHost or RemoteHost",
                "Every server you set up in the old apps, in one go.", "import"));
        var pick = await Dialogs.Show("Add a server you already have", body, 560, ("Cancel", "", "ghost"));
        switch (pick)
        {
            case "folder": await AddFolder(); break;
            case "remote": await AddRemote(); break;
            case "import": await Import(); break;
        }
    }

    /// <summary>Pick a folder, let the engine work out what is in it, confirm, open.</summary>
    public async Task AddFolder()
    {
        var r = await S.StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title = "Pick the folder your server is in" });
        var path = r.FirstOrDefault()?.TryGetLocalPath();
        if (path == null) return;
        await AddFolder(path);
    }

    public async Task AddFolder(string path)
    {
        JsonNode d;
        try { d = await Api.Post("/api/servers/detect", new { path }); }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
            return;
        }
        var existing = d.N("existing");
        if (existing != null)
        {
            Toasts.Show(existing.S("name") + " is already in your library - opening it.");
            await S.OpenInstance(existing.S("id"), "console");
            return;
        }
        if (!d.B("found"))
        {
            if (d.B("empty"))
            {
                if (await Dialogs.Confirm("No server in this folder yet",
                        d.S("error") + "\n\nLodestone can make a new one - it downloads the server and everything it needs.",
                        "Create a server", "brand"))
                    await OpenWizard(d.B("blank") ? path : null);
            }
            else Toasts.Bad(d.S("error", "That folder could not be read."));
            return;
        }
        await ConfirmAdd(d);
    }

    async Task ConfirmAdd(JsonNode d)
    {
        var sw = d.S("software");
        var swName = sw.Length > 0 ? SoftwareName(sw) : "Minecraft server";
        var name = Ui.Input(d.S("name"), "My server");
        var ramOpts = new[] { "1G", "2G", "3G", "4G", "6G", "8G", "10G", "12G", "16G" };
        var ram = Ui.Select(ramOpts.Select(x => (x, x)), ramOpts.Contains(d.S("max_ram")) ? d.S("max_ram") : "4G", null, 140);
        var java = Ui.Input(d.S("java").Length > 0 ? d.S("java") : "java", "java", mono: true);
        var javaNote = Ui.Hint(d.S("java_note"));
        var javaRow = Ui.V(8, java, javaNote);
        if (d.S("java").Length == 0 && d.I("java_need") > 0)
        {
            var need = d.I("java_need");
            Button? fetch = null;
            fetch = Ui.Btn("Download Java " + need, async () =>
            {
                fetch!.IsEnabled = false;
                var got = await FetchJava(need, javaNote);
                if (got != null) java.Text = got;
                else fetch.IsEnabled = true;
            }, "download", "sm");
            javaRow.Children.Add(fetch.Also(b => b.HorizontalAlignment = HorizontalAlignment.Left));
        }
        var jars = d.Strings("jars");
        ComboBox? jarPick = null;
        if (d.S("args_file").Length == 0 && jars.Count > 1)
            jarPick = Ui.Select(jars.Select(j => (j, j)), System.IO.Path.GetFileName(d.S("jar")), null);

        var facts = new WrapPanel();
        void Fact(string text, string icon, IBrush? fg = null) =>
            facts.Children.Add(Ui.Tag(text, icon, fg).Also(t => t.Margin = new Thickness(0, 0, 6, 6)));
        Fact(d.S("args_file").Length > 0 ? "Starts from its args file" : System.IO.Path.GetFileName(d.S("jar")), "box");
        Fact("Port " + d.I("port"), "plug");
        Fact(d.B("has_world") ? "World found" : "No world yet - made on first start", "mountain");
        Fact(d.B("eula") ? "EULA accepted" : "EULA not accepted yet", "shield", d.B("eula") ? null : Ui.Orange);

        var summary = Ui.Lead(Images.Art(sw, 64, 16), Ui.V(4,
            Ui.T((swName + " " + d.S("mc")).Trim(), null, 20, Ui.Contrast, FontWeight.ExtraBold),
            Ui.T(d.S("path"), null, 12.5, Ui.Secondary).Also(t => t.TextTrimming = TextTrimming.CharacterEllipsis),
            facts), 16);
        var body = Ui.V(0, Ui.Card(summary, 16).Also(c => c.Margin = new Thickness(0, 0, 0, 16)),
            Ui.Field("Name", name),
            jarPick == null ? null : Ui.Field("Server jar", jarPick, "Lodestone picked the most likely one; change it if it guessed wrong."),
            Ui.Cols(16, Ui.Field("Memory", ram, "The most it may use."), Ui.Field("Java", javaRow)));
        foreach (var note in d.Strings("notes"))
            if (jarPick == null || !note.StartsWith("Found ")) body.Children.Add(Ui.Hint(note));
        var res = await Dialogs.Show("Add " + d.S("name"), body, 640, ("Cancel", "", "ghost"), ("Add and open", "add", "brand"));
        if (res != "add") return;
        var jar = jarPick != null ? System.IO.Path.Combine(d.S("path"), Ui.Val(jarPick) ?? "") : d.S("jar");
        var j = await Ops.Act("/api/servers/add-local", new
        {
            path = d.S("path"), name = name.Text ?? "", jar, args_file = d.S("args_file"),
            software = sw, mc = d.S("mc"), loader_version = d.S("loader_version"),
            java = java.Text ?? "", max_ram = Ui.Val(ram) ?? "4G",
        }, null);
        if (j == null) return;
        var sid = j.N("server").S("id");
        await S.LoadShelf();
        await S.OpenInstance(sid, "console");
        Toasts.Good("Added " + (name.Text ?? "it") + (d.B("eula") ? ". Press Start when you are ready." : ". Accept the EULA, then press Start."));
    }

    /// <summary>Download a Temurin Java into Lodestone's folder, reporting in a line of text.</summary>
    static async Task<string?> FetchJava(int major, TextBlock status)
    {
        JsonNode? start;
        try { start = await Api.Post("/api/java/install", new { major }); }
        catch (Exception e)
        {
            status.Text = e.Message;
            return null;
        }
        var jid = start.S("job");
        var since = 0;
        while (true)
        {
            await Task.Delay(700);
            JsonNode job;
            try { job = (await Api.Get("/api/jobs?id=" + Api.Q(jid) + "&since=" + since)).N("job")!; }
            catch (Exception) { continue; }
            since = job.I("next");
            if (job.S("state") == "running")
            {
                status.Text = "Downloading Java " + major + "… " + job.S("pct", "0") + "%";
                continue;
            }
            if (job.S("state") == "done")
            {
                status.Text = "Java " + major + " is ready.";
                return job.N("result").S("path");
            }
            status.Text = job.S("error", "The download failed.");
            return null;
        }
    }

    /// <summary>A remote server: the login up front, then a connection test, then inside it.</summary>
    public async Task AddRemote()
    {
        var name = Ui.Input("", "My Minehut server");
        var host = Ui.Input("", "e.g. sftp.minehut.com");
        var port = Ui.Input("22", "22");
        var user = Ui.Input("", "username");
        var pass = Ui.Input("", "password", password: true);
        var root = Ui.Input("/", "/", mono: true);
        var body = Ui.V(0,
            Ui.Field("Name", name),
            Ui.Cols(16, Ui.Field("Host", host), Ui.Field("Port", port)),
            Ui.Cols(16, Ui.Field("Username", user), Ui.Field("Password", pass, "Kept in ~/.lodestone.json on this PC.")),
            Ui.Field("Server folder on the host", root, "Where server.properties is. On Minehut that is usually /server. Not sure? Leave / - the test finds it."));
        while (true)
        {
            var res = await Dialogs.Show("Connect to a remote server", body, 640, ("Cancel", "", "ghost"), ("Add and connect", "add", "brand"));
            if (res != "add") return;
            if ((host.Text ?? "").Trim().Length == 0)
            {
                Toasts.Bad("The host is needed - it is the address you log in to.");
                continue;
            }
            break;
        }
        int.TryParse(port.Text, out var p);
        var j = await Ops.Act("/api/servers/save", new JsonObject
        {
            ["kind"] = "sftp",
            ["name"] = (name.Text ?? "").Trim().Length > 0 ? name.Text!.Trim() : (host.Text ?? "Remote server").Trim(),
            ["sftp"] = new JsonObject
            {
                ["host"] = (host.Text ?? "").Trim(), ["port"] = p > 0 ? p : 22, ["user"] = (user.Text ?? "").Trim(),
                ["password"] = pass.Text ?? "", ["save_password"] = true, ["root"] = (root.Text ?? "/").Trim(),
            },
        }, null);
        if (j == null) return;
        var sid = j.N("server").S("id");
        await S.LoadShelf();
        Toasts.Show("Checking the connection…");
        JsonNode? t = null;
        try { t = await Api.Post("/api/sftp/test", new { id = sid }); }
        catch (Exception) { /* reported below */ }
        // the login works but the folder was a guess: the test found where the files really are
        var found = t.S("suggest_root");
        if (found.Length > 0 && await Ops.Act("/api/sftp/use-root", new { id = sid, root = found }, null) != null)
            t = new JsonObject { ["ok"] = true };
        if (t.B("ok"))
        {
            await S.OpenInstance(sid, "console");
            Toasts.Good("Connected" + (found.Length > 0 ? " - the server files are in " + found : "") + ".");
            return;
        }
        var failed = t?.A("steps").Items().FirstOrDefault(x => !x.B("ok"));
        await S.OpenInstance(sid, "instance");
        Toasts.Bad("Added, but the connection failed" + (failed != null ? " at \"" + failed.S("step") + "\": " + failed.S("detail") : "") +
                   " - check the login here and press Test connection.");
    }

    // ================================================================ profiles

    string ProfileSummary(JsonNode p) => ProfileSummary(p, _aikar);

    internal static string ProfileSummary(JsonNode p, string aikar)
    {
        var bits = new List<string>();
        if (p.S("min_ram").Length > 0 || p.S("max_ram").Length > 0)
            bits.Add("RAM " + p.S("min_ram", "?") + " – " + p.S("max_ram", "?"));
        if (p.S("java").Length > 0) bits.Add("Java: " + p.S("java"));
        if (p.S("extra").Length > 0) bits.Add(p.S("extra") == aikar ? "Aikar's flags" : "Custom JVM flags");
        return bits.Count == 0 ? "No Java settings - this profile only groups instances." : string.Join("  ·  ", bits);
    }

    void RenderProfiles()
    {
        if (!Built || _tab != "profiles") return;
        var host = Ui.V(18);
        host.Children.Add(Ui.Split(
            Ui.V(4, Ui.H2("Profiles"),
                Ui.Hint("A profile groups instances in your library and carries the Java settings they share: RAM, the Java " +
                        "command and JVM flags. Nothing changes on a server until you press Apply.")),
            NewProfileButton()));
        if (_profiles.Count == 0)
        {
            var presets = new WrapPanel { HorizontalAlignment = HorizontalAlignment.Center };
            foreach (var name in new[] { "Testing", "Survival", "Modded", "Proxy" })
            {
                var n = name;
                presets.Children.Add(Ui.Btn(n, () => _ = EditProfile(Preset(n)), "plus", "sm").Also(b => b.Margin = new Thickness(0, 0, 8, 8)));
            }
            var fig = Art.Figure("golem", 110);
            fig.HorizontalAlignment = HorizontalAlignment.Center;
            fig.RenderTransform = new TranslateTransform();
            fig.Classes.Add("bob");
            var t = Ui.T("No profiles yet. Start from a preset:", null, 14, Ui.Secondary);
            t.HorizontalAlignment = HorizontalAlignment.Center;
            var v = Ui.V(14, fig, t, presets);
            v.Margin = new Thickness(0, 22);
            host.Children.Add(Anim.Enter(Ui.Card(v, 24)));
        }
        else
        {
            var grid = new UniformGrid { Columns = Math.Max(1, (int)(_gridWidth / 360)) };
            grid.SizeChanged += (_, e) =>
            {
                var c = Math.Max(1, (int)(e.NewSize.Width / 360));
                if (c != grid.Columns) grid.Columns = c;
            };
            for (var i = 0; i < _profiles.Count; i++) grid.Children.Add(Anim.Enter(ProfileCard(_profiles[i]), i * 50));
            host.Children.Add(grid);
        }
        _tabBody.Content = host;
    }

    Button NewProfileButton()
    {
        var b = Ui.Btn("New profile", null, "plus", "brand");
        var menu = new MenuFlyout();
        foreach (var (name, sub) in new[]
                 {
                     ("Blank", "Name and colour only"), ("Testing", "1G – 2G, for quick local tests"),
                     ("Survival", "2G – 6G with Aikar's flags"), ("Modded", "4G – 10G with Aikar's flags"),
                     ("Proxy", "256M – 512M for Velocity or BungeeCord"),
                 })
        {
            var n = name;
            var mi = new MenuItem { Header = Ui.V(1, Ui.T(n, null, 13.5, Ui.Contrast, FontWeight.SemiBold), Ui.T(sub, null, 12, Ui.Secondary)) };
            mi.Click += (_, _) => _ = EditProfile(Preset(n));
            menu.Items.Add(mi);
        }
        b.Flyout = menu;
        return b;
    }

    JsonObject Preset(string name)
    {
        var (min, max, flags, desc, color, banner) = name switch
        {
            "Testing" => ("1G", "2G", "", "Quick local test servers", _colors[1 % _colors.Count], "valley"),
            "Survival" => ("2G", "6G", _aikar, "Everyday Paper and Spigot servers", _colors[0], "forest"),
            "Modded" => ("4G", "10G", _aikar, "Forge, NeoForge and Fabric packs", _colors[2 % _colors.Count], "jungle"),
            "Proxy" => ("256M", "512M", "", "Velocity and BungeeCord", _colors[3 % _colors.Count], ""),
            _ => ("", "", "", "", _colors[0], ""),
        };
        return new JsonObject
        {
            ["name"] = name == "Blank" ? "" : name,
            ["description"] = desc,
            ["color"] = color,
            ["banner"] = banner,
            ["java"] = "",
            ["min_ram"] = min,
            ["max_ram"] = max,
            ["extra"] = flags,
        };
    }

    Control ProfileCard(JsonNode p)
    {
        var pid = p.S("id");
        var color = Ui.Hex(p.S("color", "#1BD96A"));
        var c = Color.Parse(p.S("color", "#1BD96A"));
        var banner = Art.Banner(Art.BannerKey(p.S("banner"), pid), 96, new CornerRadius(15, 15, 0, 0), Ui.Raised, out var img);
        var tint = new Border { Background = Art.Fade(Color.FromArgb(150, c.R, c.G, c.B), Color.FromArgb(0, c.R, c.G, c.B), 0) };
        var stripe = new Border { Height = 4, Background = color, VerticalAlignment = VerticalAlignment.Top };
        var top = new Grid();
        top.Children.Add(banner);
        top.Children.Add(tint);
        top.Children.Add(stripe);

        var title = Ui.H(9, new Border { Width = 10, Height = 10, CornerRadius = new CornerRadius(5), Background = color },
            Ui.T(p.S("name"), null, 18, Ui.Contrast, FontWeight.Bold));
        var desc = Ui.T(p.S("description").Length > 0 ? p.S("description") : "No description.", null, 13, Ui.Secondary, wrap: true);

        var chips = new WrapPanel();
        void Chip(string text, string icon)
        {
            chips.Children.Add(Ui.Tag(text, icon).Also(t => t.Margin = new Thickness(0, 0, 6, 6)));
        }
        if (p.S("min_ram").Length > 0 || p.S("max_ram").Length > 0) Chip(p.S("min_ram", "?") + " – " + p.S("max_ram", "?"), "cpu");
        if (p.S("java").Length > 0) Chip(p.S("java"), "code");
        if (p.S("extra").Length > 0) Chip(p.S("extra") == _aikar ? "Aikar's flags" : "Custom flags", "sliders");
        if (chips.Children.Count == 0) Chip("Grouping only", "layers");

        var faces = new StackPanel { Orientation = Orientation.Horizontal, Spacing = -10 };
        var members = p.A("members").Items().ToList();
        foreach (var m in members.Take(6))
        {
            var face = new Border
            {
                Child = Images.Icon(m.S("icon"), 30, 9),
                CornerRadius = new CornerRadius(11),
                Padding = new Thickness(2),
                Background = Ui.Raised,
            };
            ToolTip.SetTip(face, m.S("name") + (m.B("running") ? " - running" : ""));
            faces.Children.Add(face);
        }
        var count = Ui.T(members.Count == 0 ? "No instances yet - assign one from its ⋯ menu" : Ui.Plural(members.Count, "instance"),
            null, 12.5, Ui.Secondary);
        var who = Ui.H(10, faces, count);

        var apply = Ui.Btn("Apply to " + members.Count, () => _ = ApplyProfile(pid), "check", "brand sm",
            "Copy this profile's Java settings into its local instances");
        apply.IsEnabled = members.Count > 0 && HasSettings(p);
        var actions = Ui.Split(Ui.H(6, apply, Ui.Btn("Edit", () => _ = EditProfile((JsonObject)p.Clone()), "edit", "sm")),
            Ui.IconBtn("trash", "Delete profile", () => _ = DeleteProfile(p), "soft-red", 32));

        var body = Ui.V(12, title, desc, chips, who, actions);
        body.Margin = new Thickness(18, 4, 18, 18);
        var col = new Grid { RowDefinitions = new RowDefinitions("Auto,*") };
        col.Children.Add(top);
        Grid.SetRow(body, 1);
        col.Children.Add(body);
        var card = new Border { Child = col, ClipToBounds = true, Margin = new Thickness(0, 0, 14, 14) };
        card.Classes.Add("card");
        Anim.Lift(card, 4, color);
        Anim.HoverTransform(card, img, "scale(1.07)");
        return card;
    }

    async Task ApplyProfile(string pid)
    {
        var p = Profile(pid);
        if (p == null) return;
        if (!await Dialogs.Confirm("Apply " + p.S("name") + "?", ProfileSummary(p) +
                "\n\nThese settings are written into every local instance in this profile. Remote instances are skipped.", "Apply", "brand"))
            return;
        var j = await Ops.Act("/api/profiles/apply", new { id = pid }, null);
        if (j == null) return;
        var applied = j.Strings("applied");
        var restart = j.Strings("restart");
        Toasts.Good(applied.Count == 0 ? "No local instances to apply it to."
            : "Applied to " + string.Join(", ", applied) + "." + (restart.Count > 0 ? " Restart " + string.Join(", ", restart) + " to use it." : ""));
        await Load();
    }

    async Task DeleteProfile(JsonNode p)
    {
        if (!await Dialogs.Confirm("Delete " + p.S("name") + "?",
                "Its instances stay, with the settings they have now - they just stop belonging to a profile.", "Delete"))
            return;
        await Ops.Act("/api/profiles/delete", new { id = p.S("id") }, "Profile deleted.");
        await Load();
    }

    async Task EditProfile(JsonObject p)
    {
        var name = Ui.Input(p.S("name"), "Survival, Testing, Modded…");
        var desc = Ui.Input(p.S("description"), "What these servers are for");
        var java = Ui.Input(p.S("java"), "leave blank to keep each server's", mono: true);
        var min = Ui.Input(p.S("min_ram"), "1G", mono: true);
        var max = Ui.Input(p.S("max_ram"), "4G", mono: true);
        var extra = Ui.Area(p.S("extra"), 3, "JVM flags, space separated", true);
        var color = p.S("color", _colors[0]);
        var banner = p.S("banner");

        var swatches = Ui.H(8);
        void PaintSwatches()
        {
            foreach (var ch in swatches.Children.OfType<Button>())
                ch.BorderBrush = (string)ch.Tag! == color ? Ui.Contrast : Brushes.Transparent;
        }
        foreach (var hex in _colors)
        {
            var h = hex;
            var sw = new Button
            {
                Width = 30, Height = 30, MinHeight = 30, Padding = new Thickness(0), Tag = h,
                CornerRadius = new CornerRadius(15),
                Background = Ui.Hex(h),
                BorderThickness = new Thickness(2),
                Content = new Border { Width = 22, Height = 22, CornerRadius = new CornerRadius(11), Background = Ui.Hex(h) },
            };
            sw.Click += (_, _) =>
            {
                color = h;
                PaintSwatches();
            };
            swatches.Children.Add(sw);
        }
        PaintSwatches();

        var flagRow = Ui.H(8,
            Ui.Btn("Aikar's flags", () => extra.Text = _aikar, "zap", "xs", "The G1GC tuning most Paper servers run with"),
            Ui.Btn("Clear", () => extra.Text = "", null, "ghost xs"));

        var body = Ui.V(0,
            Ui.Cols(16, Ui.Field("Name", name), Ui.Field("Colour", swatches)),
            Ui.Field("Description", desc),
            Ui.Field("Banner", Picker(Art.Banners.Select(b => (b.key, b.label)).Prepend(("", "Auto")), banner, k => banner = k, true)),
            Ui.Cols(16, Ui.Field("Min RAM", min), Ui.Field("Max RAM", max), Ui.Field("Java command", java)),
            Ui.Field("Extra JVM flags", Ui.V(8, extra, flagRow), "Blank fields leave that setting alone on each server when the profile is applied."));
        var res = await Dialogs.Show(p.S("id").Length > 0 ? "Edit profile" : "New profile", body, 680,
            ("Cancel", "", "ghost"), ("Save profile", "save", "brand"));
        if (res != "save") return;
        p["name"] = name.Text ?? "";
        p["description"] = desc.Text ?? "";
        p["java"] = java.Text ?? "";
        p["min_ram"] = min.Text ?? "";
        p["max_ram"] = max.Text ?? "";
        p["extra"] = extra.Text ?? "";
        p["color"] = color;
        p["banner"] = banner;
        p.Remove("members");
        p.Remove("count");
        if (await Ops.Act("/api/profiles/save", p, "Profile saved.") == null) return;
        _tab = "profiles";
        await Load();
    }

    /// <summary>
    /// A row of picture tiles - banners (wide) or icons (square) - with an
    /// "Auto" tile first. The chosen one is outlined in brand green.
    /// </summary>
    internal static WrapPanel Picker(IEnumerable<(string key, string label)> options, string current, Action<string> pick, bool banners)
    {
        var wrap = new WrapPanel();
        var tiles = new List<(string key, Border frame)>();
        void Paint()
        {
            foreach (var (k, f) in tiles) f.BorderBrush = k == current ? Ui.Brand : Ui.Divider;
        }
        foreach (var (key, label) in options)
        {
            var k = key;
            Control pic;
            if (k.Length == 0)
                pic = new Border
                {
                    Width = banners ? 112 : 56, Height = banners ? 62 : 56, Background = Ui.ButtonBg,
                    Child = Ui.V(2, Icons.Make("sparkles", 18, Ui.Secondary).Also(i => i.HorizontalAlignment = HorizontalAlignment.Center),
                        Ui.T("Auto", null, 11.5, Ui.Secondary).Also(t => t.HorizontalAlignment = HorizontalAlignment.Center))
                        .Also(v => v.VerticalAlignment = VerticalAlignment.Center),
                };
            else if (banners)
                pic = Art.Banner(k, 62, new CornerRadius(0)).Also(b => b.Width = 112);
            else
                pic = Images.Icon(k, 56, 0);
            var frame = new Border
            {
                Child = pic,
                CornerRadius = new CornerRadius(12),
                ClipToBounds = true,
                BorderThickness = new Thickness(2),
                Margin = new Thickness(0, 0, 8, 8),
                Cursor = new Cursor(StandardCursorType.Hand),
            };
            ToolTip.SetTip(frame, label);
            frame.PointerPressed += (_, e) =>
            {
                e.Handled = true;
                current = k;
                Paint();
                pick(k);
            };
            Anim.Lift(frame, 2);
            tiles.Add((k, frame));
            wrap.Children.Add(frame);
        }
        Paint();
        return wrap;
    }
}
