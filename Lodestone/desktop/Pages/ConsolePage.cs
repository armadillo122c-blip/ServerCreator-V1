using System.Collections.Concurrent;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using AvaloniaEdit;
using AvaloniaEdit.Document;
using AvaloniaEdit.Rendering;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// The overview, after Modrinth's server panel: stat tiles across the top, the
/// live console as a terminal card with the command line inside it, and the
/// players and session facts beside it.
/// </summary>
public sealed class ConsolePage : Page
{
    public ConsolePage(MainWindow s) : base(s) { }

    public override string Key => "console";
    public override string Title => "Console";
    public override string Icon => "console";

    readonly List<string> _lines = new();
    // what the document shows, line for line, in the current mode
    readonly List<LogShown> _shown = new();
    readonly HashSet<string> _known = new(StringComparer.OrdinalIgnoreCase);
    string _mode = "advanced";
    Border _terminal = null!;
    Button _jump = null!;
    readonly Dictionary<string, Button> _modeBtns = new();
    DispatcherTimer? _fadeTimer;
    const int FadeMs = 360;
    readonly ConcurrentQueue<string> _incoming = new();
    readonly List<string> _hist = new();
    int _hpos = -1;

    TextDocument _doc = null!;
    TextEditor _ed = null!;
    TextBlock _emptyNote = null!;
    TextBox _filter = null!, _cmd = null!;
    Button _send = null!;
    TextBlock _src = null!;
    Border _livePill = null!;
    WrapPanel _chips = null!;
    StackPanel _players = null!;
    Border _playerCount = null!;
    StackPanel _session = null!;
    TextBlock _tUp = null!, _tUpSub = null!, _tPl = null!, _tPlSub = null!, _tCmd = null!, _tCmdSub = null!, _tDc = null!, _tDcSub = null!;
    string _playerSig = "";

    protected override Control Build()
    {
        // ---- tiles
        var tiles = new UniformGrid { Columns = 4, Margin = new Thickness(0, 0, 0, 16) };
        tiles.Children.Add(Tile("Uptime", "clock", out _tUp, out _tUpSub, new Thickness(0, 0, 8, 0)));
        tiles.Children.Add(Tile("Players online", "users", out _tPl, out _tPlSub, new Thickness(8, 0, 8, 0)));
        tiles.Children.Add(Tile("Commands run", "console", out _tCmd, out _tCmdSub, new Thickness(8, 0, 8, 0)));
        tiles.Children.Add(Tile("Discord", "discord", out _tDc, out _tDcSub, new Thickness(8, 0, 0, 0)));

        // ---- terminal
        _doc = new TextDocument();
        _ed = new TextEditor
        {
            Document = _doc,
            IsReadOnly = true,
            ShowLineNumbers = false,
            FontFamily = Ui.Mono,
            FontSize = S.Prefs.Mono,
            Background = Brushes.Transparent,
            Foreground = Ui.Text,
            WordWrap = true,
            Padding = new Thickness(16, 12, 12, 12),
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
        _ed.Options.EnableHyperlinks = false;
        _ed.Options.EnableEmailHyperlinks = false;
        _ed.Options.HighlightCurrentLine = false;
        _ed.Options.AllowScrollBelowDocument = false;
        _ed.TextArea.SelectionBrush = Ui.Hex("#551BD96A");
        _ed.TextArea.SelectionForeground = null;
        _ed.TextArea.Caret.CaretBrush = Brushes.Transparent;
        // lines arrive already cleaned (colour codes become colours), so no control character is left to draw
        _ed.Options.ShowBoxForControlCharacters = false;
        _ed.TextArea.TextView.LineTransformers.Add(new ShownColorizer(this));
        _ed.TextArea.TextView.ScrollOffsetChanged += (_, _) => { if (AtEnd()) ShowJump(false); };
        _mode = S.Prefs.ConsoleMode is "simple" or "raw" ? S.Prefs.ConsoleMode : "advanced";
        _emptyNote = Ui.T("Waiting for the server to say something…", null, 13.5, Ui.Secondary);
        _emptyNote.HorizontalAlignment = HorizontalAlignment.Center;
        _emptyNote.VerticalAlignment = VerticalAlignment.Center;
        _emptyNote.IsHitTestVisible = false;
        _jump = Ui.Btn("New lines", () => { _ed.ScrollToEnd(); ShowJump(false); }, "chevron-down", "brand sm round");
        _jump.HorizontalAlignment = HorizontalAlignment.Right;
        _jump.VerticalAlignment = VerticalAlignment.Bottom;
        _jump.Margin = new Thickness(0, 0, 18, 14);
        _jump.Opacity = 0;
        _jump.IsHitTestVisible = false;
        _jump.Transitions = new Avalonia.Animation.Transitions
        {
            new Avalonia.Animation.DoubleTransition { Property = Visual.OpacityProperty, Duration = TimeSpan.FromMilliseconds(180) },
        };
        var termGrid = new Grid();
        termGrid.Children.Add(_ed);
        termGrid.Children.Add(_emptyNote);
        termGrid.Children.Add(_jump);
        var terminal = _terminal = new Border
        {
            Background = Ui.Terminal,
            CornerRadius = new CornerRadius(12),
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1),
            ClipToBounds = true,
            Child = termGrid,
            Margin = new Thickness(14, 0, 14, 0),
            Transitions = new Avalonia.Animation.Transitions
            {
                new Avalonia.Animation.DoubleTransition { Property = Visual.OpacityProperty, Duration = TimeSpan.FromMilliseconds(140) },
            },
        };

        // ---- command line
        _cmd = Ui.Input("", "Send a command…", mono: true);
        _cmd.InnerLeftContent = Ui.T("›", null, 18, Ui.Brand, FontWeight.Bold).Also(t => t.Margin = new Thickness(12, 0, 0, 2));
        _cmd.AddHandler(InputElement.KeyDownEvent, CmdKey, Avalonia.Interactivity.RoutingStrategies.Tunnel);
        ToolTip.SetTip(_cmd, "Goes over stdin, or RCON when the panel does not own the process. Up and Down walk your history.");
        _send = Ui.Btn("Send", Send, "send", "brand");
        var cmdRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), Margin = new Thickness(14, 12, 14, 0) };
        cmdRow.Children.Add(_cmd);
        _send.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(_send, 1);
        cmdRow.Children.Add(_send);
        _chips = new WrapPanel { Margin = new Thickness(14, 10, 14, 14) };

        var body = new Grid { RowDefinitions = new RowDefinitions("*,Auto,Auto") };
        body.Children.Add(terminal);
        Grid.SetRow(cmdRow, 1);
        body.Children.Add(cmdRow);
        Grid.SetRow(_chips, 2);
        body.Children.Add(_chips);

        _src = Ui.T("—", null, 12.5, Ui.Secondary);
        _filter = Ui.Input("", "Filter", width: 170);
        _filter.MinHeight = 32;
        _filter.Padding = new Thickness(8, 5, 10, 5);
        _filter.InnerLeftContent = Icons.Make("search", 15, Ui.Secondary).Also(i => i.Margin = new Thickness(10, 0, 0, 0));
        _filter.TextChanged += (_, _) => Rebuild();
        _livePill = Ui.Pill("connecting", "warn");
        var sizer = Ui.H(2,
            Ui.IconBtn("minus", "Smaller text", () => S.BumpMono(-1), "ghost sm", 30).Also(b => b.Content = Ui.T("A−", null, 12, null, FontWeight.Bold)),
            Ui.IconBtn("plus", "Bigger text", () => S.BumpMono(1), "ghost sm", 30).Also(b => b.Content = Ui.T("A+", null, 13, null, FontWeight.Bold)));
        var modes = new Border
        {
            Background = Ui.ButtonBg,
            CornerRadius = new CornerRadius(9),
            Padding = new Thickness(3),
            Child = Ui.H(2,
                ModeBtn("simple", "Simple", "Chat, joins, deaths, commands, start and stop, errors - and nothing else"),
                ModeBtn("advanced", "Advanced", "Every line, cleaned up: colours instead of codes, a short header, plugins and players picked out"),
                ModeBtn("raw", "Raw", "Exactly what the server printed, escape codes and all")),
        };
        var head = Ui.V(10,
            Ui.Split(Ui.H(10, Ui.H2("Console"), _src), Ui.H(8, _livePill, Ui.Btn("Clear", Clear, "trash", "ghost sm"))),
            Ui.Split(modes, Ui.H(8, _filter, sizer)));
        var consoleCard = Ui.FillCard(head, body);

        // ---- side
        _playerCount = Ui.Pill("0", "", false);
        _players = Ui.V(6);
        var playersCard = Ui.Card(Ui.V(12, Ui.Split(Ui.H2("Players"), _playerCount), _players), 18);
        _session = Ui.V(0);
        var sessionCard = Ui.Card(Ui.V(8, Ui.H2("Session"), _session), 18);
        var side = new ScrollViewer
        {
            Content = Ui.V(16, playersCard, sessionCard),
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };

        var main = new Grid { ColumnDefinitions = new ColumnDefinitions("*,16,310") };
        main.Children.Add(consoleCard);
        Grid.SetColumn(side, 2);
        main.Children.Add(side);

        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,*"), Margin = new Thickness(28, 16, 28, 24) };
        root.Children.Add(tiles);
        Grid.SetRow(main, 1);
        root.Children.Add(main);

        S.SizesChanged += () => _ed.FontSize = S.Prefs.Mono;
        DispatcherTimer.Run(() =>
        {
            Flush();
            return true;
        }, TimeSpan.FromMilliseconds(120));
        RenderPlayers(new JsonArray());
        Rebuild();
        return root;
    }

    static Border Tile(string label, string icon, out TextBlock value, out TextBlock sub, Thickness margin)
    {
        value = Ui.T("—", null, 24, Ui.Contrast, FontWeight.Bold);
        sub = Ui.T("", null, 12.5, Ui.Secondary);
        sub.TextTrimming = TextTrimming.CharacterEllipsis;
        var badge = new Border
        {
            Width = 40,
            Height = 40,
            CornerRadius = new CornerRadius(12),
            Background = Ui.ButtonBg,
            Child = Icons.Make(icon, 20, Ui.Text).Also(i => i.HorizontalAlignment = HorizontalAlignment.Center),
            VerticalAlignment = VerticalAlignment.Top,
        };
        var text = Ui.V(2, Ui.T(label, null, 13, Ui.Secondary, FontWeight.SemiBold), value, sub);
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        g.Children.Add(text);
        Grid.SetColumn(badge, 1);
        g.Children.Add(badge);
        var card = Ui.Card(g, 16);
        card.Margin = margin;
        return card;
    }

    // ================================================================ lines

    public async Task<long> LoadRecent()
    {
        var recent = await Api.Get("/api/recent");
        var arr = recent.A("lines");
        _lines.Clear();
        while (_incoming.TryDequeue(out _)) { }
        long last = 0;
        foreach (var l in arr.Items())
        {
            if (l is JsonArray pair && pair.Count >= 2)
            {
                last = J.Str(pair[0]) is var s && long.TryParse(s, out var n) ? n : last;
                _lines.Add(J.Str(pair[1]));
                Learn(_lines[^1]);
            }
        }
        if (Built) Rebuild();
        return last;
    }

    public void Enqueue(string text) => _incoming.Enqueue(text);

    public override Task OnShow()
    {
        RenderChips();
        return Task.CompletedTask;
    }

    void Flush()
    {
        if (_incoming.IsEmpty) return;
        var batch = new List<string>();
        while (_incoming.TryDequeue(out var t)) batch.Add(t);
        _lines.AddRange(batch);
        foreach (var l in batch) Learn(l);
        if (_lines.Count > 4000)
        {
            _lines.RemoveRange(0, _lines.Count - 3000);
            Rebuild();
            return;
        }
        var add = Shown(batch, Environment.TickCount64);
        if (add.Count == 0) return;
        var atEnd = AtEnd();
        _doc.BeginUpdate();
        foreach (var l in add)
        {
            _doc.Insert(_doc.TextLength, (_shown.Count == 0 ? "" : "\n") + l.Text);
            _shown.Add(l);
        }
        if (_shown.Count > 2600)
        {
            var drop = _shown.Count - 2000;
            var cut = _doc.GetLineByNumber(drop + 1);
            _doc.Remove(0, cut.Offset);
            _shown.RemoveRange(0, drop);
        }
        _doc.EndUpdate();
        _emptyNote.IsVisible = false;
        if (atEnd) _ed.ScrollToEnd();
        else ShowJump(true);
        StartFade();
    }

    /// <summary>The lines of a batch as this mode shows them, after the filter.</summary>
    List<LogShown> Shown(IEnumerable<string> raw, long born)
    {
        var f = (_filter.Text ?? "").Trim();
        var out_ = new List<LogShown>();
        foreach (var l in raw)
        {
            var s = LogView.Render(l, _mode, _known);
            if (s == null) continue;
            if (f.Length > 0 && !s.Text.Contains(f, StringComparison.OrdinalIgnoreCase)) continue;
            s.Born = born;
            out_.Add(s);
        }
        return out_;
    }

    /// <summary>Players named in joins, so a plugin's "RANK Name » hi" is read as their chat.</summary>
    void Learn(string line)
    {
        var p = LogView.Parse(line);
        if (p.Kind is LogView.Kind.Join or LogView.Kind.Chat or LogView.Kind.Leave && p.Who.Length > 0) _known.Add(p.Who);
    }

    Button ModeBtn(string key, string label, string tip)
    {
        var b = Ui.Btn(label, () => SetMode(key), null, "tab", tip);
        b.MinWidth = 76;
        b.Classes.Set("active", key == _mode);
        _modeBtns[key] = b;
        return b;
    }

    void SetMode(string mode)
    {
        if (mode == _mode) return;
        _mode = mode;
        S.Prefs.ConsoleMode = mode;
        S.Prefs.Save();
        foreach (var (k, b) in _modeBtns) b.Classes.Set("active", k == mode);
        // a quick cross-fade, so the switch reads as one view changing rather than a flicker
        _terminal.Opacity = 0;
        DispatcherTimer.RunOnce(() =>
        {
            Rebuild();
            _terminal.Opacity = 1;
        }, TimeSpan.FromMilliseconds(130));
    }

    void ShowJump(bool on)
    {
        _jump.Opacity = on ? 1 : 0;
        _jump.IsHitTestVisible = on;
    }

    /// <summary>New lines fade in: redraw the view while any line is younger than the fade.</summary>
    void StartFade()
    {
        if (_fadeTimer != null) return;
        _fadeTimer = new DispatcherTimer(TimeSpan.FromMilliseconds(16), DispatcherPriority.Render, (_, _) =>
        {
            _ed.TextArea.TextView.Redraw();
            var now = Environment.TickCount64;
            if (_shown.Count == 0 || now - _shown[^1].Born > FadeMs)
            {
                _fadeTimer?.Stop();
                _fadeTimer = null;
            }
        });
        _fadeTimer.Start();
    }

    bool AtEnd() => _ed.VerticalOffset + _ed.ViewportHeight >= _ed.ExtentHeight - 40;

    void Rebuild()
    {
        var f = (_filter.Text ?? "").Trim();
        var all = Shown(_lines, 0);
        var show = all.Skip(Math.Max(0, all.Count - 2000)).ToList();
        _shown.Clear();
        _shown.AddRange(show);
        _doc.Text = string.Join("\n", show.Select(s => s.Text));
        ShowJump(false);
        _emptyNote.Text = f.Length > 0 ? "Nothing matches that filter."
            : _mode == "simple" && _lines.Count > 0 ? "Nothing to show in Simple mode yet - chat, joins and server events appear here."
            : "Waiting for the server to say something…";
        _emptyNote.IsVisible = _doc.TextLength == 0;
        Dispatcher.UIThread.Post(() => _ed.ScrollToEnd(), DispatcherPriority.Background);
    }

    public void Clear()
    {
        _lines.Clear();
        Rebuild();
    }

    public void SetLive(bool ok)
    {
        if (!Built) return;
        Ui.SetPill(_livePill, ok ? "live" : "reconnecting", ok ? "on" : "warn");
    }

    // ================================================================ commands

    void CmdKey(object? sender, KeyEventArgs e)
    {
        if (e.Key == Avalonia.Input.Key.Enter)
        {
            e.Handled = true;
            Send();
        }
        else if (e.Key == Avalonia.Input.Key.Up && _hist.Count > 0)
        {
            e.Handled = true;
            _hpos = Math.Min(_hpos + 1, _hist.Count - 1);
            _cmd.Text = _hist[_hpos];
            _cmd.CaretIndex = _cmd.Text.Length;
        }
        else if (e.Key == Avalonia.Input.Key.Down)
        {
            e.Handled = true;
            _hpos = Math.Max(_hpos - 1, -1);
            _cmd.Text = _hpos < 0 ? "" : _hist[_hpos];
            _cmd.CaretIndex = _cmd.Text?.Length ?? 0;
        }
    }

    async void Send()
    {
        var cmd = (_cmd.Text ?? "").Trim();
        if (cmd.Length == 0) return;
        _hist.Insert(0, cmd);
        _hpos = -1;
        _cmd.Text = "";
        try { await Api.Post("/api/command", new { command = cmd }); }
        catch (Exception e) { Toasts.Bad(e.Message); }
    }

    public void RenderChips()
    {
        if (!Built) return;
        _chips.Children.Clear();
        foreach (var m in S.Features.A("macros").Items())
        {
            var name = m.S("name");
            if (name.Length == 0) continue;
            var b = Ui.Btn(name, async () => await Ops.Act("/api/macro", new { name }), "zap", "xs round");
            b.Margin = new Thickness(0, 0, 6, 0);
            ToolTip.SetTip(b, m.S("commands"));
            _chips.Children.Add(b);
        }
        _chips.IsVisible = _chips.Children.Count > 0;
    }

    // ================================================================ status

    public override void OnStatus(JsonNode s)
    {
        if (!Built) return;
        var running = s.B("running");
        var srv = s.N("server");
        var tr = s.N("transport");
        _src.Text = srv.S("kind") == "local" ? (running ? "live process output" : "not running") : "tailing logs/latest.log";
        _cmd.IsEnabled = s.B("can_command");
        _send.IsEnabled = s.B("can_command");
        _cmd.Watermark = s.B("can_command")
            ? "Send a command…"
            : "Start the server (or turn on RCON) to send commands";

        _tUp.Text = s.L("uptime") > 0 ? s.S("uptime_h") : "—";
        _tUpSub.Text = s.D("done_ms") > 0 ? "started in " + s.S("done_ms") + "s" : (running ? "starting…" : s.S("state", "offline"));
        _tPl.Text = s.S("player_count", "0");
        var players = s.A("players");
        _tPlSub.Text = players.Count == 0 ? "nobody online" : string.Join(", ", players.Items().Take(3).Select(p => J.Str(p))) + (players.Count > 3 ? "…" : "");
        _tCmd.Text = s.N("counters").S("cmds", "0");
        _tCmdSub.Text = "this session";
        var dc = s.N("discord");
        _tDc.Text = dc.S("sent", "0");
        _tDcSub.Text = Ui.Plural(s.I("hooks"), "webhook") + (dc.I("failed") > 0 ? " · " + dc.S("failed") + " failed" : "");

        foreach (var pl in players.Items()) _known.Add(J.Str(pl));
        RenderPlayers(players);
        Ui.SetPill(_playerCount, s.S("player_count", "0"), s.I("player_count") > 0 ? "on" : "", false);

        _session.Children.Clear();
        _session.Children.Add(Ui.KRow("Server", srv.S("name")));
        _session.Children.Add(Ui.KRow("Kind", srv.S("kind") == "local" ? "local process" : "remote (sftp)"));
        _session.Children.Add(Ui.KRow("Where", tr.S("root").Length > 0 ? tr.S("root") : tr.S("host", "—")));
        _session.Children.Add(Ui.KRow("Started in", s.D("done_ms") > 0 ? s.S("done_ms") + "s" : "—"));
        _session.Children.Add(Ui.KRow("Webhooks", s.S("hooks", "0")));
        _session.Children.Add(Ui.KRow("Discord sent", dc.S("sent", "0") + (dc.I("failed") > 0 ? " (" + dc.S("failed") + " failed)" : "")));
        _session.Children.Add(Ui.KRow("Transferred", "↓" + s.S("down") + "  ↑" + s.S("up")));
        var bk = s.N("backup");
        _session.Children.Add(Ui.KRow("Backup", bk.B("busy") ? bk.S("msg", "working…") : bk.S("msg", "idle"),
            bk.B("busy") ? Ui.Orange : null));
    }

    void RenderPlayers(JsonArray list)
    {
        var names = list.Items().Select(p => J.Str(p)).ToList();
        var sig = string.Join(",", names);
        if (sig == _playerSig && _players.Children.Count > 0) return;
        _playerSig = sig;
        _players.Children.Clear();
        if (names.Count == 0)
        {
            _players.Children.Add(Ui.Empty("Nobody online.", "users"));
            return;
        }
        foreach (var p in names)
        {
            var name = p;
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto") };
            var head = Images.Slot(Images.Head(name), 30, 8);
            row.Children.Add(head);
            var nm = Ui.MonoText(name, 13, Ui.Contrast);
            nm.Margin = new Thickness(10, 0, 6, 0);
            nm.VerticalAlignment = VerticalAlignment.Center;
            nm.TextTrimming = TextTrimming.CharacterEllipsis;
            Grid.SetColumn(nm, 1);
            row.Children.Add(nm);
            var acts = Ui.H(2,
                Ui.IconBtn("user-plus", "Op", () => PAct(name, "op"), "ghost", 28),
                Ui.IconBtn("logout", "Kick", () => PAct(name, "kick"), "ghost", 28),
                Ui.IconBtn("ban", "Ban", () => PAct(name, "ban"), "soft-red", 28));
            acts.Opacity = 0.35;
            Grid.SetColumn(acts, 2);
            row.Children.Add(acts);
            var wrap = new Border { Child = row, Padding = new Thickness(6), CornerRadius = new CornerRadius(10), Background = Brushes.Transparent };
            wrap.PointerEntered += (_, _) => { wrap.Background = Ui.ButtonBg; acts.Opacity = 1; };
            wrap.PointerExited += (_, _) => { wrap.Background = Brushes.Transparent; acts.Opacity = 0.35; };
            _players.Children.Add(wrap);
        }
    }

    async void PAct(string player, string action)
    {
        if (action is "ban" or "kick")
        {
            var r = await Dialogs.Prompt((action == "ban" ? "Ban " : "Kick ") + player, "Reason (optional)", "",
                action == "ban" ? "Ban" : "Kick");
            if (r == null) return;
            var j = await Ops.Act("/api/player", new { player, action, reason = r }, null);
            if (j != null) Toasts.Good(j.S("out").Length > 0 ? j.S("out") : "Done.");
            return;
        }
        await Ops.Act("/api/player", new { player, action });
    }

    // ================================================================ colours

    /// <summary>Paints each shown line from its LogShown: the colours LogView worked out, faded in while new.</summary>
    sealed class ShownColorizer : DocumentColorizingTransformer
    {
        readonly ConsolePage _page;

        public ShownColorizer(ConsolePage page) => _page = page;

        static IBrush Faded(IBrush b, double k) =>
            k >= 1 ? b : b is ISolidColorBrush s ? new SolidColorBrush(s.Color, s.Opacity * k) : b;

        protected override void ColorizeLine(DocumentLine line)
        {
            var i = line.LineNumber - 1;
            if (i < 0 || i >= _page._shown.Count || line.Length == 0) return;
            var shown = _page._shown[i];
            var age = shown.Born == 0 ? FadeMs : Environment.TickCount64 - shown.Born;
            var k = Math.Clamp(age / (double)FadeMs, 0, 1);
            k = 1 - Math.Pow(1 - k, 3);                     // ease out
            if (k < 1)
                ChangeLinePart(line.Offset, line.EndOffset, el => el.TextRunProperties.SetForegroundBrush(Faded(LogView.Plain, k)));
            var len = line.Length;
            foreach (var seg in shown.Segs)
            {
                var a = Math.Max(0, seg.Start);
                var b = Math.Min(len, seg.End);
                if (b <= a) continue;
                var brush = Faded(seg.Brush, k);
                var bold = seg.Bold;
                ChangeLinePart(line.Offset + a, line.Offset + b, el =>
                {
                    el.TextRunProperties.SetForegroundBrush(brush);
                    if (bold)
                        el.TextRunProperties.SetTypeface(new Typeface(el.TextRunProperties.Typeface.FontFamily, FontStyle.Normal, FontWeight.Bold));
                });
            }
        }
    }
}
