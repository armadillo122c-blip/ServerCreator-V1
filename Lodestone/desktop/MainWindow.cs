using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.Pages;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop;

/// <summary>
/// The shell, laid out like the Modrinth app: a frame with the title bar and a
/// navigation column, and the page on a darker sheet with a rounded corner.
/// It owns the connection - status stream, server shelf, mode - and hands the
/// pages what they need.
///
/// It works in two places, the way a launcher does. At home the sidebar is the
/// library - Instances, Network, Settings and the servers to jump back into.
/// Opening an instance steps inside it: the sidebar becomes that server's tabs
/// under a card naming it, with "All instances" to step back out, and the
/// title bar spells out where you are.
/// </summary>
public sealed class MainWindow : Window
{
    public readonly Options Opt;
    public readonly Prefs Prefs = Prefs.Load();
    public readonly Engine Engine = new();
    public readonly LiveStream Stream = new();

    public JsonNode State { get; private set; } = new JsonObject();
    public List<JsonNode> Servers { get; private set; } = new();
    public JsonNode Features { get; set; } = new JsonObject();
    public JsonArray Events { get; private set; } = new();
    public string Mode { get; private set; } = "advanced";
    public string Current { get; private set; } = "";
    public bool Connected { get; private set; }

    /// <summary>True while a server's own tabs are showing rather than the library.</summary>
    public bool InInstance { get; private set; }

    public event Action? SizesChanged;

    readonly List<Page> _pages = new();
    readonly Dictionary<string, Button> _nav = new();
    readonly Dictionary<string, Border> _badges = new();
    readonly Dictionary<string, TextBlock> _groupHeads = new();

    public ConsolePage Console { get; }

    // parts
    LayoutTransformControl _scale = null!;
    ContentControl _host = null!;
    ServerHeader _header = null!;
    StackPanel _crumbs = null!;
    string _crumbSig = "";
    StackPanel _libNav = null!, _instNav = null!, _shelfList = null!;
    TextBlock _shelfHead = null!;
    Button _instCard = null!;
    ContentControl _instIcon = null!;
    TextBlock _instName = null!, _instSub = null!;
    Border _instDot = null!;
    string _instIconFor = "";
    Border _modeTag = null!;
    TextBlock _modeText = null!;
    Border _splash = null!;
    StackPanel _splashBody = null!;
    Border _liveDot = null!;
    TextBlock _liveText = null!;
    bool _quitting;
    bool _reviving;
    readonly List<DateTime> _revived = new();
    Grid _root = null!;
    bool _introShown;
    Border? _maxGlyph;
    readonly List<Control> _navContents = new();

    static readonly bool IsMac = RuntimeInformation.IsOSPlatform(OSPlatform.OSX);
    static readonly bool IsWin = RuntimeInformation.IsOSPlatform(OSPlatform.Windows);

    public MainWindow(Options opt)
    {
        Opt = opt;
        Title = "Lodestone";
        try { Icon = new WindowIcon(AssetLoader.Open(new Uri("avares://Lodestone/Assets/lodestone.ico"))); }
        catch (Exception) { /* no icon is not worth failing over */ }
        Width = Prefs.Width;
        Height = Prefs.Height;
        MinWidth = 1000;
        MinHeight = 640;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        if (Prefs.Maximized) WindowState = WindowState.Maximized;
        Background = Ui.Frame;
        Foreground = Ui.Text;
        FontFamily = Application.Current!.TryFindResource("UiFont", out var f) && f is FontFamily ff ? ff : FontFamily.Default;
        FontSize = 14;

        // Draw into the title bar like the Modrinth app. On Windows the window
        // buttons are Lodestone's own, over the pixel field; macOS keeps its
        // traffic lights, and on Linux, where this is unsupported, the normal
        // title bar simply stays.
        ExtendClientAreaToDecorationsHint = true;
        ExtendClientAreaChromeHints = IsWin
            ? Avalonia.Platform.ExtendClientAreaChromeHints.NoChrome
            : Avalonia.Platform.ExtendClientAreaChromeHints.PreferSystemChrome;
        ExtendClientAreaTitleBarHeightHint = 52;
        Anim.Enabled = opt.ShotsDir == null;
        Sounds.Init(Prefs);
        Sounds.Enabled = opt.ShotsDir == null;
        // every button, switch and menu item clicks; the sidebar and tab bars have their own tick
        Button.ClickEvent.AddClassHandler<Button>((b, _) =>
            Sounds.Play(b.Classes.Contains("nav") || b.Classes.Contains("tab") ? "tab"
                        : b is ToggleButton ? "toggle" : "click"));
        MenuItem.ClickEvent.AddClassHandler<MenuItem>((_, _) => Sounds.Play("click"));

        Console = new ConsolePage(this);
        _pages.AddRange(new Page[]
        {
            Console, new PerfPage(this), new FilesPage(this), new PropertiesPage(this), new BackupsPage(this), new ContentPage(this),
            new InstancePage(this), new ToolsPage(this), new AutomationPage(this), new AssistantPage(this), new DiscordPage(this),
            new ServersPage(this), new AgentPage(this), new CodePage(this), new NetworkPage(this), new ApiPage(this), new SettingsPage(this),
        });

        Content = Build();
        ApplySizes(save: false);

        Stream.Line += (_, text) =>
        {
            Console.Enqueue(text);
            if (ChatLine.IsMatch(text)) Sounds.Play("chat");
            else if (text.StartsWith("!!!", StringComparison.Ordinal)) Sounds.Play("alert");
        };
        Stream.Status += j => Dispatcher.UIThread.Post(() => OnStatus(j));
        Stream.Connected += ok => Dispatcher.UIThread.Post(() =>
        {
            SetLive(ok);
            if (!ok) _ = Revive();
        });

        AddHandler(KeyDownEvent, (_, e) => Dialogs.HandleKey(e), Avalonia.Interactivity.RoutingStrategies.Tunnel);
        Closing += OnClosing;
        if (opt.ShotsDir == null) Opened += async (_, _) => await Boot();
    }

    // ================================================================ layout

    Control Build()
    {
        var titleBar = BuildTitleBar();
        var side = BuildSidebar();

        _header = new ServerHeader(this);
        _host = new ContentControl
        {
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            VerticalContentAlignment = VerticalAlignment.Stretch,
        };
        var content = new Grid { RowDefinitions = new RowDefinitions("Auto,*") };
        content.Children.Add(_header.View);
        Grid.SetRow(_host, 1);
        content.Children.Add(_host);

        var sheet = new Border
        {
            Background = Ui.Bg,
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1, 1, 0, 0),
            CornerRadius = new CornerRadius(20, 0, 0, 0),
            ClipToBounds = true,
            Child = content,
        };

        var body = new Grid { ColumnDefinitions = new ColumnDefinitions("236,*") };
        body.Children.Add(side);
        Grid.SetColumn(sheet, 1);
        body.Children.Add(sheet);

        var main = new Grid { RowDefinitions = new RowDefinitions("52,*") };
        main.Children.Add(titleBar);
        Grid.SetRow(body, 1);
        main.Children.Add(body);

        var toasts = new StackPanel
        {
            Spacing = 10,
            HorizontalAlignment = HorizontalAlignment.Right,
            VerticalAlignment = VerticalAlignment.Bottom,
            Margin = new Thickness(24),
        };
        Toasts.Init(toasts);
        var dialogs = new Grid();
        Dialogs.Init(dialogs);

        _splashBody = new StackPanel { Spacing = 16, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
        _splash = new Border { Background = Ui.Bg, Child = _splashBody };
        SplashMessage("Starting…");

        var root = _root = new Grid();
        root.Children.Add(main);
        root.Children.Add(toasts);
        root.Children.Add(dialogs);
        root.Children.Add(_splash);
        _scale = new LayoutTransformControl { Child = root };
        return _scale;
    }

    Control BuildTitleBar()
    {
        _modeText = Ui.T("advanced", null, 12, Ui.Secondary, FontWeight.SemiBold);
        _modeTag = new Border
        {
            Background = Ui.ButtonBg,
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(9, 2),
            Child = _modeText,
            Cursor = new Cursor(StandardCursorType.Hand),
            VerticalAlignment = VerticalAlignment.Center,
        };
        ToolTip.SetTip(_modeTag, "Switch between Simple and Advanced");
        _modeTag.PointerPressed += (_, e) =>
        {
            e.Handled = true;
            ChooseMode();
        };

        // on a dark pill, so the glowing dots behind never fight the name
        var brand = new Border
        {
            Background = Ui.Hex("#E01D1F23"),
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(8, 4, 6, 4),
            BoxShadow = BoxShadows.Parse("0 0 14 2 #CC1D1F23"),
            Child = Ui.H(10, Logo.Make(26), Ui.T("Lodestone", null, 18, Ui.Contrast, FontWeight.ExtraBold), _modeTag),
            VerticalAlignment = VerticalAlignment.Center,
        };

        // Where you are: Instances > the server > the tab. Each step back is a click.
        _crumbs = Ui.H(2);
        _crumbs.VerticalAlignment = VerticalAlignment.Center;
        var crumbPill = new Border
        {
            Background = Ui.Hex("#E01D1F23"),
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(4, 3),
            BoxShadow = BoxShadows.Parse("0 0 14 2 #CC1D1F23"),
            Child = _crumbs,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(14, 0, 0, 0),
        };

        _liveDot = new Border { Width = 8, Height = 8, CornerRadius = new CornerRadius(4), Background = Ui.Secondary };
        _liveText = Ui.T("connecting", null, 12.5, Ui.Secondary, FontWeight.SemiBold);
        // a dark pill, so the dots behind never fight the text
        var live = new Border
        {
            Background = Ui.Hex("#D916181C"),
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(10, 4, 12, 4),
            Child = Ui.H(7, _liveDot, _liveText),
            VerticalAlignment = VerticalAlignment.Center,
        };
        ToolTip.SetTip(live, "The live console and status connection to the engine");

        var bar = new Grid
        {
            ColumnDefinitions = new ColumnDefinitions("Auto,Auto,*,Auto,Auto"),
            Margin = new Thickness(IsMac ? 84 : 18, 0, IsWin ? 12 : 18, 0),
            VerticalAlignment = VerticalAlignment.Center,
        };
        bar.Children.Add(brand);
        Grid.SetColumn(crumbPill, 1);
        bar.Children.Add(crumbPill);
        var gear = new Button { Content = Icons.Make("settings", 15, Ui.Contrast) };
        gear.Classes.Add("cap");
        ToolTip.SetTip(gear, "Lodestone settings");
        gear.Click += (_, _) => ShowView("settings");
        var right = Ui.H(8, gear, MuteButton(), live);
        Grid.SetColumn(right, 3);
        bar.Children.Add(right);
        if (IsWin)
        {
            var caps = CaptionButtons();
            caps.Margin = new Thickness(14, 0, 0, 0);
            Grid.SetColumn(caps, 4);
            bar.Children.Add(caps);
        }

        // The animated cube field across the whole bar: square cubes whose
        // colours flow through a gradient and twinkle.
        var field = new PixelField
        {
            Start = -20, Cell = 6, Gap = 1, Density = 0.14, Strength = 0.95, FrameMs = 60,
            Palette = PixelField.Aurora,
        };
        var layered = new Grid { ClipToBounds = true };
        layered.Children.Add(field);
        layered.Children.Add(bar);

        var host = new Border { Background = Brushes.Transparent, Child = layered };
        host.PointerPressed += (_, e) =>
        {
            if (e.Source is Visual v && (v == host || v == bar || v == layered || v is PixelField || v is TextBlock || v is Canvas || v is Viewbox)
                && e.GetCurrentPoint(this).Properties.IsLeftButtonPressed)
            {
                if (e.ClickCount == 2)
                    WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
                else
                    BeginMoveDrag(e);
            }
        };
        return host;
    }

    /// <summary>A speaker key that mutes and unmutes the sound effects.</summary>
    Control MuteButton()
    {
        static Control Glyph(bool on)
        {
            var c = new Canvas { Width = 16, Height = 16 };
            c.Children.Add(new Avalonia.Controls.Shapes.Path
            {
                Data = Geometry.Parse("M2 6 H5 L9 3 V13 L5 10 H2 Z"),
                Fill = Ui.Contrast,
            });
            c.Children.Add(new Avalonia.Controls.Shapes.Path
            {
                Data = Geometry.Parse(on ? "M11 5.5 C12.6 7 12.6 9 11 10.5 M12.8 3.8 C15.4 6.2 15.4 9.8 12.8 12.2"
                                         : "M11 6 L15 10 M15 6 L11 10"),
                Stroke = on ? Ui.Contrast : Ui.Red,
                StrokeThickness = 1.6,
                StrokeLineCap = PenLineCap.Round,
            });
            return c;
        }
        var b = new Button { Content = Glyph(Sounds.On) };
        b.Classes.Add("cap");
        void Paint()
        {
            b.Content = Glyph(Sounds.On);
            ToolTip.SetTip(b, Sounds.On ? "Sound on - click to mute" : "Muted - click for sound");
        }
        Paint();
        b.Click += (_, _) => Sounds.SetOn(!Sounds.On);
        Sounds.Changed += Paint;
        return b;
    }

    /// <summary>Minimise, maximise and close, drawn as rounded keys; close is the white one.</summary>
    Control CaptionButtons()
    {
        Button Cap(Control glyph, string tip, Action a)
        {
            var b = new Button { Content = glyph };
            b.Classes.Add("cap");
            ToolTip.SetTip(b, tip);
            b.Click += (_, _) => a();
            return b;
        }
        var min = Cap(new Border { Width = 11, Height = 2, CornerRadius = new CornerRadius(1), Background = Ui.Contrast }, "Minimise",
            () => WindowState = WindowState.Minimized);
        _maxGlyph = new Border
        {
            Width = 10, Height = 10, CornerRadius = new CornerRadius(2.5),
            BorderThickness = new Thickness(1.6), BorderBrush = Ui.Contrast,
        };
        var max = Cap(_maxGlyph, "Maximise",
            () => WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized);
        var close = Cap(Icons.Make("x", 15, Ui.Hex("#16181C")), "Close", Close);
        close.Classes.Add("close");
        close.PointerEntered += (_, _) => close.Content = Icons.Make("x", 15, Ui.Contrast);
        close.PointerExited += (_, _) => close.Content = Icons.Make("x", 15, Ui.Hex("#16181C"));
        PropertyChanged += (_, e) =>
        {
            if (e.Property != WindowStateProperty || _maxGlyph == null) return;
            var maxed = WindowState == WindowState.Maximized;
            _maxGlyph.Width = maxed ? 9 : 10;
            _maxGlyph.Height = maxed ? 9 : 10;
            ToolTip.SetTip(max, maxed ? "Restore" : "Maximise");
        };
        return Ui.H(6, min, max, close);
    }

    Control BuildSidebar()
    {
        // ---- at home: the library, Lodestone's own pages, and your servers
        _libNav = new StackPanel { Spacing = 2, Margin = new Thickness(12, 10, 12, 12) };
        foreach (var p in _pages.Where(p => !p.ServerScoped)) _libNav.Children.Add(NavButton(p));
        _shelfHead = Ui.Eyebrow("Jump back in").Also(t => t.Margin = new Thickness(12, 20, 0, 6));
        _shelfList = new StackPanel { Spacing = 2 };
        _libNav.Children.Add(_shelfHead);
        _libNav.Children.Add(_shelfList);
        _libNav.Children.Add(SideButton("New server", "plus", () => _ = NewServer(), Ui.Brand));

        // ---- inside an instance: its card, then its tabs, grouped
        _instNav = new StackPanel { Spacing = 2, Margin = new Thickness(12, 10, 12, 12), IsVisible = false };
        _instNav.Children.Add(SideButton("All instances", "arrow-left", GoHome));
        _instCard = InstanceCard();
        _instCard.Margin = new Thickness(0, 6, 0, 2);
        _instNav.Children.Add(_instCard);
        var serverPages = _pages.Where(p => p.ServerScoped).ToList();
        foreach (var group in serverPages.Select(p => p.Group).Distinct())
        {
            var head = Ui.Eyebrow(group).Also(t => t.Margin = new Thickness(12, 16, 0, 6));
            _groupHeads[group] = head;
            _instNav.Children.Add(head);
            foreach (var p in serverPages.Where(p => p.Group == group)) _instNav.Children.Add(NavButton(p));
        }

        var both = new Panel();
        both.Children.Add(_libNav);
        both.Children.Add(_instNav);
        return new ScrollViewer { Content = both, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
    }

    /// <summary>A sidebar entry that is an action rather than a page.</summary>
    Button SideButton(string text, string icon, Action click, IBrush? iconColor = null)
    {
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*") };
        var ic = Icons.Make(icon, 19, iconColor);
        ic.Margin = new Thickness(0, 0, 12, 0);
        g.Children.Add(ic);
        var label = new TextBlock { Text = text, VerticalAlignment = VerticalAlignment.Center };
        Grid.SetColumn(label, 1);
        g.Children.Add(label);
        var b = new Button { Content = g };
        b.Classes.Add("nav");
        b.Click += (_, _) => click();
        _navContents.Add(g);
        Anim.HoverTransform(b, ic, "translateX(3px) scale(1.08)", "translateX(0px) scale(1)", 180);
        return b;
    }

    /// <summary>The open server at the top of its tabs; clicking it switches to another.</summary>
    Button InstanceCard()
    {
        _instIcon = new ContentControl { Width = 40, Height = 40, VerticalAlignment = VerticalAlignment.Center };
        _instName = Ui.T("—", null, 14.5, Ui.Contrast, FontWeight.Bold);
        _instName.TextTrimming = TextTrimming.CharacterEllipsis;
        _instDot = new Border { Width = 7, Height = 7, CornerRadius = new CornerRadius(4), Background = Ui.Ring, VerticalAlignment = VerticalAlignment.Center };
        _instSub = Ui.T("", null, 12, Ui.Secondary);
        _instSub.TextTrimming = TextTrimming.CharacterEllipsis;
        var text = Ui.V(3, _instName, Ui.H(6, _instDot, _instSub));
        text.Margin = new Thickness(10, 0, 4, 0);
        text.VerticalAlignment = VerticalAlignment.Center;
        var chevron = Icons.Make("chevron-down", 15, Ui.Secondary);
        chevron.VerticalAlignment = VerticalAlignment.Center;
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto") };
        g.Children.Add(_instIcon);
        Grid.SetColumn(text, 1);
        g.Children.Add(text);
        Grid.SetColumn(chevron, 2);
        g.Children.Add(chevron);
        var b = new Button { Content = g };
        b.Classes.Add("instcard");
        ToolTip.SetTip(b, "Switch to another instance");
        return b;
    }

    /// <summary>Every server, running ones first, for the switcher menus.</summary>
    MenuFlyout SwitcherMenu()
    {
        var menu = new MenuFlyout();
        foreach (var s in Servers.OrderByDescending(Up).ThenBy(x => x.S("name"), StringComparer.OrdinalIgnoreCase))
        {
            var id = s.S("id");
            var name = Ui.T(s.S("name"), null, 13.5, Ui.Contrast, s.B("active") ? FontWeight.Bold : FontWeight.SemiBold);
            var row = Ui.H(10, Images.ServerIcon(ServerLook.From(s), 26, 7), Ui.V(1, name, Ui.T(ServerLine(s), null, 12, Ui.Secondary)));
            if (s.B("active")) row.Children.Add(Icons.Make("check", 15, Ui.Brand).Also(i => i.VerticalAlignment = VerticalAlignment.Center));
            var mi = new MenuItem { Header = row };
            mi.Click += (_, _) => _ = OpenInstance(id);
            menu.Items.Add(mi);
        }
        if (Servers.Count > 0) menu.Items.Add(new Separator());
        var all = new MenuItem { Header = "All instances", Icon = Icons.Make("home", 16, Ui.Secondary) };
        all.Click += (_, _) => GoHome();
        menu.Items.Add(all);
        var add = new MenuItem { Header = "New server…", Icon = Icons.Make("plus", 16, Ui.Brand) };
        add.Click += (_, _) => _ = NewServer();
        menu.Items.Add(add);
        return menu;
    }

    static bool Up(JsonNode s) => s.B("running") || (s.S("kind") != "local" && s.S("state") == "online");

    /// <summary>"Running · Paper 1.21.4", the line under a server's name.</summary>
    public static string ServerLine(JsonNode? s)
    {
        var sw = s.S("software");
        var what = sw.Length > 0 ? (ServersPage.SoftwareName(sw) + " " + s.S("mc")).Trim()
                                 : s.S("kind") == "local" ? "Local" : "Remote";
        var local = s.S("kind") == "local";
        return (s.B("running") ? "Running" : local ? "Stopped" : s.S("state") == "online" ? "Connected" : "Offline") + "  ·  " + what;
    }

    /// <summary>The servers to jump back into, newest activity first.</summary>
    void PaintShelf()
    {
        _shelfList.Children.Clear();
        var list = Servers.OrderByDescending(s => s.B("active")).ThenByDescending(Up)
            .ThenByDescending(s => s.L("last_started")).ThenBy(s => s.S("name"), StringComparer.OrdinalIgnoreCase).Take(8);
        foreach (var s in list)
        {
            var id = s.S("id");
            var g = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto") };
            var icon = Images.ServerIcon(ServerLook.From(s), 24, 6);
            icon.Margin = new Thickness(0, 0, 10, 0);
            icon.VerticalAlignment = VerticalAlignment.Center;
            g.Children.Add(icon);
            var label = new TextBlock { Text = s.S("name"), VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis };
            Grid.SetColumn(label, 1);
            g.Children.Add(label);
            if (Up(s))
            {
                var dot = new Border { Width = 8, Height = 8, CornerRadius = new CornerRadius(4), Background = Ui.Brand, VerticalAlignment = VerticalAlignment.Center };
                dot.Classes.Add("pulse");
                Grid.SetColumn(dot, 2);
                g.Children.Add(dot);
            }
            var b = new Button { Content = g };
            b.Classes.Add("nav");
            ToolTip.SetTip(b, s.S("name") + "\n" + ServerLine(s));
            b.Click += (_, _) => _ = OpenInstance(id, "console");
            _shelfList.Children.Add(b);
        }
        _shelfHead.IsVisible = Servers.Count > 0;
        _instCard.Flyout = SwitcherMenu();
    }

    /// <summary>The card naming the open instance, kept current from the status stream.</summary>
    void PaintInstanceCard()
    {
        var srv = Servers.FirstOrDefault(x => x.B("active"));
        var st = State.N("server");
        _instName.Text = st.S("name", srv.S("name", "—"));
        ToolTip.SetTip(_instCard, _instName.Text + " - click to switch to another instance");
        var running = State.B("running");
        var state = State.S("state");
        var local = st.S("kind", srv.S("kind", "local")) == "local";
        var up = running || (!local && state == "online");
        _instDot.Background = up ? Ui.Brand : state == "error" ? Ui.Red : Ui.Ring;
        _instDot.Classes.Set("pulse", up);
        var sw = st.S("software", srv.S("software"));
        var what = sw.Length > 0 ? (ServersPage.SoftwareName(sw) + " " + st.S("mc", srv.S("mc"))).Trim()
                                 : local ? "Local" : "Remote";
        _instSub.Text = (running ? "Running" : local ? "Stopped" : up ? "Connected" : "Offline") + " · " + what;
        var sig = srv.S("id") + "|" + srv.S("icon") + "|" + srv.S("software") + "|" + srv.B("has_icon");
        if (srv != null && sig != _instIconFor)
        {
            _instIconFor = sig;
            _instIcon.Content = Images.ServerIcon(ServerLook.From(srv), 40, 10);
        }
    }

    /// <summary>Instances › My server ▾ › Console - every step but the last is a way back.</summary>
    void PaintCrumbs(bool force = false)
    {
        var page = _pages.FirstOrDefault(p => p.Key == Current);
        var srv = Servers.FirstOrDefault(x => x.B("active"));
        var name = State.N("server").S("name", srv.S("name"));
        var sig = string.Join("|", Current, srv.S("id"), name, srv.S("icon"), srv.B("has_icon"), Servers.Count);
        if (!force && sig == _crumbSig) return;
        _crumbSig = sig;
        _crumbs.Children.Clear();
        Button Crumb(Control content, Action? click, string tip)
        {
            var b = new Button { Content = content };
            b.Classes.Add("crumb");
            if (click != null) b.Click += (_, _) => click();
            ToolTip.SetTip(b, tip);
            return b;
        }
        TextBlock Sep() => Ui.T("›", null, 16, Ui.Secondary, FontWeight.Bold).Also(t =>
        {
            t.VerticalAlignment = VerticalAlignment.Center;
            t.Margin = new Thickness(2, 0, 2, 2);
        });
        TextBlock Here(string t) => Ui.T(t, null, 13.5, Ui.Contrast, FontWeight.Bold).Also(x =>
        {
            x.VerticalAlignment = VerticalAlignment.Center;
            x.Margin = new Thickness(6, 0, 10, 0);
        });
        var atHome = page == null || page.Key == "servers";
        var home = Crumb(Ui.H(7, Icons.Make("home", 15, atHome ? Ui.Brand : null),
            new TextBlock { Text = "Instances", VerticalAlignment = VerticalAlignment.Center }), GoHome, "All your servers");
        _crumbs.Children.Add(home);
        if (atHome) return;
        if (page!.ServerScoped && srv != null)
        {
            var label = new TextBlock { Text = name, VerticalAlignment = VerticalAlignment.Center, MaxWidth = 220, TextTrimming = TextTrimming.CharacterEllipsis };
            var pick = Crumb(Ui.H(7, Images.ServerIcon(ServerLook.From(srv), 20, 5), label, Icons.Make("chevron-down", 13, Ui.Secondary)),
                null, "Switch to another instance");
            pick.Flyout = SwitcherMenu();
            _crumbs.Children.Add(Sep());
            _crumbs.Children.Add(pick);
        }
        _crumbs.Children.Add(Sep());
        _crumbs.Children.Add(Here(page.Title));
    }

    Button NavButton(Page p)
    {
        var badgeText = Ui.T("", null, 11.5, Ui.Secondary, FontWeight.SemiBold);
        var badge = new Border
        {
            Background = Ui.ButtonBg,
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(7, 1),
            Child = badgeText,
            IsVisible = false,
            VerticalAlignment = VerticalAlignment.Center,
        };
        _badges[p.Key] = badge;
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto") };
        var icon = Icons.Make(p.Icon, 19);
        icon.Margin = new Thickness(0, 0, 12, 0);
        g.Children.Add(icon);
        var label = new TextBlock { Text = p.Title, VerticalAlignment = VerticalAlignment.Center };
        Grid.SetColumn(label, 1);
        g.Children.Add(label);
        Grid.SetColumn(badge, 2);
        g.Children.Add(badge);
        var b = new Button { Content = g };
        b.Classes.Add("nav");
        b.Click += (_, _) => ShowView(p.Key);
        _nav[p.Key] = b;
        _navContents.Add(g);
        // the icon leans in under the pointer
        Anim.HoverTransform(b, icon, "translateX(3px) scale(1.08)", "translateX(0px) scale(1)", 180);
        return b;
    }

    public void SetBadge(string key, string? text, IBrush? fg = null)
    {
        if (!_badges.TryGetValue(key, out var b)) return;
        b.IsVisible = !string.IsNullOrEmpty(text);
        if (b.Child is TextBlock t)
        {
            t.Text = text ?? "";
            t.Foreground = fg ?? Ui.Secondary;
        }
    }

    // ================================================================ splash

    void SplashMessage(string msg)
    {
        _splashBody.Children.Clear();
        var logo = Logo.Make(64);
        logo.HorizontalAlignment = HorizontalAlignment.Center;
        _splashBody.Children.Add(logo);
        _splashBody.Children.Add(Ui.T("Lodestone", null, 26, Ui.Contrast, FontWeight.ExtraBold).Also(t => t.HorizontalAlignment = HorizontalAlignment.Center));
        var bar = new ProgressBar { IsIndeterminate = true, Width = 220, Height = 4, MinHeight = 4, Foreground = Ui.Brand, Background = Ui.ButtonBg };
        _splashBody.Children.Add(bar);
        _splashBody.Children.Add(Ui.T(msg, null, 13.5, Ui.Secondary).Also(t => t.HorizontalAlignment = HorizontalAlignment.Center));
        _splash.IsVisible = true;
    }

    void SplashError(EngineException e)
    {
        _splashBody.Children.Clear();
        var body = Ui.V(14,
            Ui.H(12, Icons.Make("alert", 26, Ui.Red), Ui.H2("Lodestone could not start its engine")),
            Ui.T(e.Message, null, 14, Ui.Text, wrap: true));
        if (!string.IsNullOrWhiteSpace(e.Log))
        {
            body.Children.Add(Ui.Eyebrow("Engine log"));
            body.Children.Add(Ui.Code(e.Log!));
        }
        body.Children.Add(Ui.Hint("The engine is lodestone.py, run headless. It needs Python 3.8+ unless a bundled " +
                                  "engine sits in the \"engine\" folder next to this app. Its log is at " + Engine.LogPath + "."));
        body.Children.Add(Ui.H(8,
            Ui.Btn("Try again", async () => await Boot(), "refresh", "brand"),
            Ui.Btn("Quit", Close, null, "ghost")));
        var card = Ui.Card(body, 24);
        card.MaxWidth = 720;
        card.Margin = new Thickness(24);
        _splashBody.Children.Add(card);
        _splash.IsVisible = true;
    }

    // ================================================================ boot

    public async Task Boot()
    {
        // The intro plays once per launch, over the splash, while the engine connects.
        Intro? intro = null;
        if (!_introShown && Anim.Enabled && Prefs.Intro)
        {
            intro = new Intro();
            _root.Children.Add(intro.View);
            intro.Play();
        }
        _introShown = true;
        SplashMessage("Starting…");
        try
        {
            await Engine.Connect(Opt, new Progress<string>(m =>
            {
                SplashMessage(m);
                intro?.Status(m);
            }));
        }
        catch (EngineException e)
        {
            if (intro != null) await intro.Close(_root);
            SplashError(e);
            return;
        }
        intro?.Status("Connected - loading your servers…");
        await AfterConnect();
        if (intro != null)
        {
            intro.Status("Ready");
            await intro.Played;
            await intro.Close(_root);
        }
        // the sidebar settles in, one entry after another
        for (var i = 0; i < _navContents.Count; i++) Anim.Reveal(_navContents[i], i * 28);
    }

    /// <summary>Everything after the engine answers. Also used by --shots.</summary>
    public async Task AfterConnect()
    {
        Connected = true;
        long last = 0;
        try
        {
            await LoadShelf();
            last = await Console.LoadRecent();
            OnStatus(await Api.Get("/api/status"));
            Features = await Api.Get("/api/features");
            Console.RenderChips();
        }
        catch (Exception e)
        {
            Toasts.Bad("Could not reach the panel: " + e.Message);
        }
        try { Events = (await Api.Get("/api/events")).A("items"); }
        catch (Exception) { Events = FallbackEvents(); }

        _splash.IsVisible = false;
        var me = await Ops.Get("/api/me", false);
        var mode = me.S("mode");
        if (mode.Length == 0)
        {
            // First run. No question to answer before anything works: someone
            // new starts with the simple set of tabs (the welcome screen has the
            // switch for the rest), and someone who already has servers keeps everything.
            mode = Servers.Count == 0 ? "simple" : "advanced";
            if (Opt.ShotsDir == null) _ = SaveMode(mode);
        }
        ApplyMode(mode);
        // Home first: pick the server, then work inside it.
        ShowView("servers");
        if (Opt.ShotsDir == null)
        {
            if (!Engine.Owned) _ = CheckEngineVersion();
            Stream.Start(last);
            DispatcherTimer.Run(() =>
            {
                _ = _header.RefreshMinehut();
                return true;
            }, TimeSpan.FromSeconds(15));
        }
    }

    /// <summary>
    /// The live connection dropped. A blip reconnects by itself; but when
    /// nothing answers on the port any more - the engine exited, or this
    /// window had adopted another Lodestone's engine and that Lodestone was
    /// closed - every page would fail with "could not reach" until restart.
    /// Start an engine again instead, at most three times in five minutes.
    /// </summary>
    async Task Revive()
    {
        if (_reviving || _quitting || !Connected || Opt.ShotsDir != null) return;
        _reviving = true;
        try
        {
            await Task.Delay(1500);
            if (_quitting || await Engine.IsLodestone(Engine.Port, 1500)) return;
            var why = Engine.Owned
                ? Engine.ExitCode is int code ? "stopped (exit code " + code + ")" : "stopped answering"
                : "this window was using belonged to another Lodestone, which was closed";
            _revived.RemoveAll(t => DateTime.UtcNow - t > TimeSpan.FromMinutes(5));
            if (_revived.Count >= 3)
            {
                await EngineDown("The Lodestone engine " + why + " again. It has stopped three times in a few minutes, " +
                                 "so it was not restarted.");
                return;
            }
            _revived.Add(DateTime.UtcNow);
            var wasOurs = Engine.Owned;
            _liveText.Text = "restarting the engine";
            try
            {
                await Engine.Connect(Opt, new Progress<string>(_ => { }));
            }
            catch (EngineException e)
            {
                await EngineDown("The Lodestone engine " + why + ", and could not be started again: " + e.Message);
                return;
            }
            Toasts.Show("The Lodestone engine " + why + " - it has been started again." +
                        (wasOurs ? " What happened is in engine.prev.log." : ""));
            await LoadShelf();
            Stream.Start(await Console.LoadRecent());
            ShowView(Current.Length > 0 ? Current : "servers");
        }
        finally
        {
            _reviving = false;
        }
    }

    async Task EngineDown(string message)
    {
        var log = Engine.LogTail(30, File.Exists(Engine.PrevLogPath) && Engine.ExitCode == null ? Engine.PrevLogPath : null);
        var body = Ui.V(12, Ui.T(message, null, 13.5, Ui.Text, wrap: true),
            Ui.T("The engine's log, " + Engine.LogPath + ":", null, 12.5, Ui.Secondary, wrap: true),
            log.Length > 0 ? Ui.Code(log) : Ui.Hint("(empty)"));
        var r = await Dialogs.Show("The engine stopped", body, 640,
            ("Try again", "again", "brand"), ("Copy the log", "copy", ""), ("Close", "", ""));
        if (r == "copy") await Copy(log);
        if (r == "again")
        {
            _revived.Clear();
            await Revive();
        }
    }

    /// <summary>
    /// After connecting: an engine without the assistant's routes is an older
    /// Lodestone that was already running, which this window adopted. Say so,
    /// or every new page fails in a way that looks like a bug in it.
    /// </summary>
    async Task CheckEngineVersion()
    {
        try
        {
            await Api.Get("/api/agent/state");
        }
        catch (ApiException e) when (e.Status == 404)
        {
            await Dialogs.Info("An older Lodestone is running", Ui.V(10,
                Ui.T("This window connected to a Lodestone engine that was already running on port " + Engine.Port +
                     ", and that engine is an older version without the AI Assistant, Code and Performance pages.",
                    null, 13.5, Ui.Text, wrap: true),
                Ui.T("Close the other Lodestone (stop its servers first), then open this one again.", null, 13.5, Ui.Text, wrap: true)));
        }
        catch (Exception)
        {
            // anything else is reported where it happens
        }
    }

    static JsonArray FallbackEvents()
    {
        // Only for an engine older than /api/events; the list mirrors EVENTS in lodestone.py.
        var a = new JsonArray();
        foreach (var (k, n, d) in new[]
        {
            ("chat", "Chat", "Every in-game chat message"), ("join_leave", "Joins & leaves", "Players connecting and disconnecting"),
            ("first_join", "First-time players", "Someone joining for the very first time"), ("death", "Deaths", "Death messages, with the cause"),
            ("advancement", "Advancements", "Advancements, challenges and goals"), ("start_stop", "Start & stop", "Server coming up and going down cleanly"),
            ("crash", "Crashes", "Unexpected exits and auto-restarts"), ("lag", "Lag warnings", "Cannot keep up / overloaded / low TPS"),
            ("backup", "Backups", "Backup finished, failed or restored"), ("console", "Console errors", "ERROR / SEVERE lines from the log"),
            ("punish", "Bans & kicks", "Punishments seen in the server log"), ("ingame_cmd", "Player commands", "Commands players run in-game"),
            ("command", "Panel commands", "Commands run from this panel or RCON"), ("player_act", "Panel moderation", "Op, kick, ban and whitelist done here"),
            ("files", "File changes", "Edits, uploads and deletes via the panel"), ("schedule", "Scheduled tasks", "Automation firing on a timer"),
        })
            a.Add(new JsonArray(k, n, d));
        return a;
    }

    // ================================================================ status

    static readonly System.Text.RegularExpressions.Regex ChatLine =
        new(@"\]:\s+(?:\[Not Secure\]\s*)?<[A-Za-z0-9_.]{2,17}>", System.Text.RegularExpressions.RegexOptions.Compiled);

    string? _sndSid;
    bool _sndUp;
    HashSet<string>? _sndPlayers;

    /// <summary>Start, stop, join and leave sounds, from changes between status updates.</summary>
    void StatusSounds(JsonNode s)
    {
        var sid = s.N("server").S("id");
        var up = s.B("running");
        var players = s.Strings("players").ToHashSet();
        if (sid != _sndSid || _sndPlayers == null)
        {
            // a different server (or the first update): this is the baseline, not news
            _sndSid = sid;
            _sndUp = up;
            _sndPlayers = players;
            return;
        }
        if (up != _sndUp) Sounds.Play(up ? "start" : "stop");
        else if (players.Except(_sndPlayers).Any()) Sounds.Play("join");
        else if (_sndPlayers.Except(players).Any()) Sounds.Play("leave");
        _sndUp = up;
        _sndPlayers = players;
    }

    public void OnStatus(JsonNode s)
    {
        StatusSounds(s);
        State = s;
        _header.Update(s);
        PaintInstanceCard();
        PaintCrumbs();

        SetBadge("console", s.I("player_count") > 0 ? s.S("player_count") : null, Ui.Brand);
        SetBadge("discord", s.I("hooks") > 0 ? s.S("hooks") : null);
        var ai = s.N("ai");
        if (ai != null)
            SetBadge("ai", ai.B("ready") ? (ai.B("automod") ? "automod" : "ready") : "off",
                ai.B("ready") ? Ui.Brand : null);
        PaintNetBadge(s.N("hosting"));
        PaintUpdate(s.N("update"));
        if (s.Has("perf_mode")) ApplyPerfMode(s.B("perf_mode"));
        foreach (var p in _pages) p.OnStatus(s);
    }

    bool _updateToldOf;

    /// <summary>An update downloaded in the background: say so once, and badge Settings until it is used.</summary>
    void PaintUpdate(JsonNode? u)
    {
        if (u == null) return;
        var ready = u.B("ready");
        SetBadge("settings", ready ? "update" : u.S("state") == "available" ? "new" : null, ready ? Ui.Brand : Ui.Orange);
        if (ready && !_updateToldOf)
        {
            _updateToldOf = true;
            Toasts.Good("A Lodestone update is installed - restart from Settings > Updates to use it.");
        }
    }

    /// <summary>
    /// Restart into an installed update: the engine quits, updater.py waits for
    /// this window to close, rebuilds it if its source changed, and starts
    /// Lodestone again.
    /// </summary>
    public async Task RestartForUpdate()
    {
        JsonNode? r;
        async Task<JsonNode?> Ask(bool force)
        {
            try { return await Api.Post("/api/update/restart", new { force, app_pid = Environment.ProcessId }); }
            catch (ApiException e) when (e.Body != null) { return e.Body; }
            catch (Exception e)
            {
                Toasts.Bad(e.Message);
                return null;
            }
        }
        r = await Ask(false);
        if (r == null) return;
        if (r.B("needs_force"))
        {
            var go = await Dialogs.Confirm("Stop the servers and restart?",
                r.S("error") + " Players on them are disconnected; the world is saved first.", "Stop and restart");
            if (!go) return;
            r = await Ask(true);
            if (r == null) return;
        }
        if (!r.B("ok"))
        {
            Toasts.Bad(r.S("error", "Could not restart."));
            return;
        }
        Toasts.Show("Restarting Lodestone into the update…");
        await Task.Delay(700);
        Finish();
    }

    void PaintNetBadge(JsonNode? h)
    {
        if (h == null || h.IsFalse("available")) { SetBadge("network", "n/a"); return; }
        if (h.B("busy")) { SetBadge("network", "…", Ui.Orange); return; }
        if (h.S("mode") == "internet")
        {
            if (h.B("online")) SetBadge("network", "public", Ui.Brand);
            else if (h.S("connectivity_state") is "unknown" or "checking")
                SetBadge("network", h.B("stale") ? "recheck" : "unknown", Ui.Orange);
            else SetBadge("network", "not public", Ui.Red);
        }
        else SetBadge("network", h.S("mode") == "local" ? "local" : h.S("mode") == "tunnel" ? "tunnel" : "lan");
    }

    void SetLive(bool ok)
    {
        _liveDot.Background = ok ? Ui.Brand : Ui.Orange;
        _liveDot.Classes.Set("pulse", ok);
        _liveText.Text = ok ? "live" : "reconnecting";
        _liveText.Foreground = ok ? Ui.Brand : Ui.Orange;
        Console.SetLive(ok);
    }

    // ================================================================ servers

    public async Task LoadShelf()
    {
        var j = await Api.Get("/api/servers");
        Servers = j.A("items").Items().ToList();
        PaintShelf();
        PaintInstanceCard();
        PaintCrumbs(true);
        // the last server removed from inside it: nothing left to be inside
        if (InInstance && Servers.Count == 0) GoHome();
    }

    public async Task<bool> SwitchServer(string id)
    {
        if (Servers.FirstOrDefault(s => s.B("active")).S("id") == id) return true;
        var j = await Ops.Act("/api/servers/activate", new { id }, null);
        if (j == null)
        {
            await LoadShelf();
            return false;
        }
        Stream.Stop();
        long last = 0;
        try
        {
            last = await Console.LoadRecent();
            await LoadShelf();
            OnStatus(await Api.Get("/api/status"));
            Features = await Api.Get("/api/features");
            Console.RenderChips();
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
        if (Opt.ShotsDir == null) Stream.Start(last);
        _header.RefreshIcon();
        foreach (var p in _pages) p.OnServerSwitched();
        var page = _pages.FirstOrDefault(p => p.Key == Current);
        if (page is { ServerScoped: true }) await page.OnShow();
        return true;
    }

    /// <summary>
    /// Step inside a server: make it the selected one and show one of its tabs -
    /// the one asked for, the same tab when switching from inside another
    /// server, or the console.
    /// </summary>
    public async Task OpenInstance(string id, string? page = null)
    {
        var target = page ?? (InInstance ? Current : "console");
        if (!await SwitchServer(id)) return;
        ShowView(target);
    }

    /// <summary>Back out to the library.</summary>
    public void GoHome() => ShowView("servers");

    /// <summary>The Create server wizard, from anywhere.</summary>
    public async Task NewServer()
    {
        ShowView("servers");
        if (GetPage("servers") is ServersPage lib) await lib.OpenWizard();
    }

    public void RefreshIcon()
    {
        Images.ForgetServerIcons();
        _header.RefreshIcon();
        _instIconFor = "";
        PaintInstanceCard();
        PaintCrumbs(true);
    }

    // ================================================================ views

    public void ShowView(string key)
    {
        var page = _pages.FirstOrDefault(p => p.Key == key);
        if (page == null) return;
        if (Mode == "simple" && page.Advanced) page = page.ServerScoped ? _pages[0] : _pages.First(p => p.Key == "servers");
        // a server's tabs need a server: with none yet, home is the only place to be
        if (page.ServerScoped && Connected && Servers.Count == 0) page = _pages.First(p => p.Key == "servers");
        var old = _pages.FirstOrDefault(p => p.Key == Current);
        if (old != null && old != page) old.OnHide();
        Current = page.Key;
        foreach (var (k, b) in _nav) b.Classes.Set("active", k == page.Key);
        if (InInstance != page.ServerScoped)
        {
            InInstance = page.ServerScoped;
            _libNav.IsVisible = !InInstance;
            _instNav.IsVisible = InInstance;
            Anim.PageIn(InInstance ? _instNav : _libNav);
        }
        PaintCrumbs();
        _header.View.IsVisible = page.ServerScoped;
        var changed = _host.Content != page.View;
        _host.Content = page.View;
        if (changed) Anim.PageIn(page.View);
        if (State.Has("server")) page.OnStatus(State);
        _ = page.OnShow();
    }

    public Page? GetPage(string key) => _pages.FirstOrDefault(p => p.Key == key);

    /// <summary>Open a file of a workspace in the code editor, at a line.</summary>
    public void OpenCode(string workspace, string path, int line)
    {
        ShowView("code");
        if (GetPage("code") is CodePage cp) _ = cp.OpenAt(workspace, path, line);
    }

    /// <summary>Open the assistant on a workspace with a request typed in (and sent, if asked).</summary>
    public async Task AskAssistant(string workspace, string text, bool send)
    {
        ShowView("dev");
        if (GetPage("dev") is AgentPage ap) await ap.Ask(workspace, text, send);
    }

    public bool PerfMode { get; private set; }

    /// <summary>
    /// Development Performance Mode, the window's half: no animations, the
    /// title bar's pixel field stops redrawing, and the looping status
    /// animations pause (the theme keys them on the 'calm' class). The
    /// engine's half - one assistant task, model kept loaded, indexing and
    /// background jobs paused - is in the engine.
    /// </summary>
    public void ApplyPerfMode(bool on)
    {
        if (PerfMode == on) return;
        PerfMode = on;
        Anim.Enabled = !on && Opt.ShotsDir == null;
        Classes.Set("calm", on);
    }

    // ================================================================ mode

    public void ApplyMode(string mode)
    {
        Mode = mode == "simple" ? "simple" : "advanced";
        foreach (var p in _pages)
            if (_nav.TryGetValue(p.Key, out var b))
                b.IsVisible = !(Mode == "simple" && p.Advanced);
        // A heading with nothing visible under it goes too.
        foreach (var (group, head) in _groupHeads)
            head.IsVisible = _pages.Any(p => p.Group == group && !(Mode == "simple" && p.Advanced));
        _modeText.Text = Mode;
        var cur = _pages.FirstOrDefault(p => p.Key == Current);
        if (Mode == "simple" && cur is { Advanced: true }) ShowView(cur.ServerScoped ? "console" : "servers");
    }

    public async void ChooseMode()
    {
        Border Choice(string title, string sub, string mode, string cls)
        {
            var row = Ui.Row(Ui.TitleSub(title, sub),
                Ui.Btn("Use " + title.ToLowerInvariant(), () => Dialogs.Close(mode), null, cls + " sm"));
            if (Mode == mode) row.Classes.Add("active");
            return row;
        }
        var body = Ui.V(10,
            Choice("Simple", "Console, files, properties, backups, mods & plugins and instance settings. Everything you need to run a server, nothing else.", "simple", "brand"),
            Choice("Advanced", "Everything: log search, plugins and worlds, automation, the AI assistant, Discord webhooks and the REST API.", "advanced", ""),
            Ui.Hint("You can switch any time - click the word next to the Lodestone name, or change it in Settings. " +
                    "Nothing is deleted either way; Simple only hides things.").Also(h => h.Margin = new Thickness(0, 6, 0, 0)));
        var pick = await Dialogs.Show("How much do you want to see?", body, ("Later", "", "ghost"));
        if (pick is "simple" or "advanced") await SetMode(pick);
    }

    static async Task SaveMode(string mode)
    {
        try { await Api.Post("/api/ui/mode", new { mode }); }
        catch (Exception) { /* asked again next time */ }
    }

    public async Task SetMode(string mode)
    {
        ApplyMode(mode);
        try { await Api.Post("/api/ui/mode", new { mode }); }
        catch (Exception e) { Toasts.Bad(e.Message); }
        Toasts.Good(mode == "simple" ? "Simple mode - the extra tabs are hidden." : "Advanced mode - everything is on.");
    }

    // ================================================================ sizes

    public void ApplySizes(bool save = true)
    {
        var s = Prefs.Ui / 14.0;
        _scale.LayoutTransform = Math.Abs(s - 1) < 0.001 ? null : new ScaleTransform(s, s);
        SizesChanged?.Invoke();
        if (save) Prefs.Save();
    }

    public void BumpMono(int d)
    {
        Prefs.Mono = Math.Clamp(Prefs.Mono + d, 10, 28);
        ApplySizes();
    }

    // ================================================================ files

    /// <summary>Save a download from the engine where the user picks.</summary>
    public async Task SaveDownload(string apiPath, string suggestedName)
    {
        var file = await StorageProvider.SaveFilePickerAsync(new Avalonia.Platform.Storage.FilePickerSaveOptions
        {
            Title = "Save " + suggestedName,
            SuggestedFileName = suggestedName,
        });
        if (file == null) return;
        Toasts.Show("Downloading " + suggestedName + "…");
        try
        {
            await using (var dest = await file.OpenWriteAsync())
                await Api.DownloadTo(apiPath, dest);
            Toasts.Good("Saved " + (file.TryGetLocalPath() ?? file.Name));
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
    }

    public async Task Copy(string text)
    {
        try
        {
            if (Clipboard != null) await Clipboard.SetTextAsync(text);
            Toasts.Good("Copied " + text);
        }
        catch (Exception)
        {
            Toasts.Bad("Could not copy - select it by hand.");
        }
    }

    public static void OpenUrl(string url)
    {
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (Exception)
        {
            Toasts.Bad("Could not open " + url);
        }
    }

    // ================================================================ closing

    async void OnClosing(object? sender, WindowClosingEventArgs e)
    {
        Prefs.Maximized = WindowState == WindowState.Maximized;
        if (WindowState == WindowState.Normal)
        {
            Prefs.Width = Width;
            Prefs.Height = Height;
        }
        Prefs.Save();
        // An engine this window adopted was running before it opened, so it
        // keeps running after it closes. One this window started goes with it.
        if (_quitting || !Engine.Owned || !Connected) return;
        e.Cancel = true;
        await QuitAll();
    }

    /// <summary>
    /// Stop the engine and close. If a Minecraft server is running under it,
    /// ask first: killing a server because a window closed would be a nasty
    /// surprise, so keeping it running in the background is offered too.
    /// </summary>
    public async Task QuitAll()
    {
        var r = await Engine.Quit(false);
        if (r == null || r.B("ok"))
        {
            Finish();
            return;
        }
        if (r.B("needs_force"))
        {
            var choice = await Dialogs.Show("A Minecraft server is still running",
                Ui.T("Lodestone can keep running in the background so the server stays up - open Lodestone again to " +
                     "get back to it. Or it can stop the server cleanly, then quit.", null, 14, Ui.Text, wrap: true),
                ("Cancel", "", "ghost"), ("Keep it running", "keep", ""), ("Stop server and quit", "force", "danger"));
            if (choice == "keep") Finish();
            else if (choice == "force")
            {
                Toasts.Show("Stopping the server, then quitting…");
                await Engine.Quit(true);
                Finish();
            }
            return;
        }
        Toasts.Bad(r.S("error", "Could not stop the engine."));
    }

    void Finish()
    {
        _quitting = true;
        Stream.Stop();
        Close();
    }
}
