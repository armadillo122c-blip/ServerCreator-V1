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
        _ed.TextArea.TextView.LineTransformers.Add(new LogColorizer());
        _ed.TextArea.TextView.ElementGenerators.Add(new CodeHider());
        _emptyNote = Ui.T("Waiting for the server to say something…", null, 13.5, Ui.Secondary);
        _emptyNote.HorizontalAlignment = HorizontalAlignment.Center;
        _emptyNote.VerticalAlignment = VerticalAlignment.Center;
        _emptyNote.IsHitTestVisible = false;
        var termGrid = new Grid();
        termGrid.Children.Add(_ed);
        termGrid.Children.Add(_emptyNote);
        var terminal = new Border
        {
            Background = Ui.Terminal,
            CornerRadius = new CornerRadius(12),
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1),
            ClipToBounds = true,
            Child = termGrid,
            Margin = new Thickness(14, 0, 14, 0),
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
        var head = Ui.Split(
            Ui.H(10, Ui.H2("Console"), _src),
            Ui.H(8, _filter, sizer, _livePill, Ui.Btn("Clear", Clear, "trash", "ghost sm")));
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
        if (_lines.Count > 4000)
        {
            _lines.RemoveRange(0, _lines.Count - 3000);
            Rebuild();
            return;
        }
        var f = (_filter.Text ?? "").Trim();
        var add = f.Length == 0 ? batch : batch.Where(l => l.Contains(f, StringComparison.OrdinalIgnoreCase)).ToList();
        if (add.Count == 0) return;
        var atEnd = AtEnd();
        _doc.BeginUpdate();
        foreach (var l in add)
            _doc.Insert(_doc.TextLength, (_doc.TextLength == 0 ? "" : "\n") + l);
        if (_doc.LineCount > 2600)
        {
            var cut = _doc.GetLineByNumber(_doc.LineCount - 2000);
            _doc.Remove(0, cut.Offset);
        }
        _doc.EndUpdate();
        _emptyNote.IsVisible = false;
        if (atEnd) _ed.ScrollToEnd();
    }

    bool AtEnd() => _ed.VerticalOffset + _ed.ViewportHeight >= _ed.ExtentHeight - 40;

    void Rebuild()
    {
        var f = (_filter.Text ?? "").Trim();
        var use = f.Length == 0 ? _lines : _lines.Where(l => l.Contains(f, StringComparison.OrdinalIgnoreCase)).ToList();
        var show = use.Skip(Math.Max(0, use.Count - 2000));
        _doc.Text = string.Join("\n", show);
        _emptyNote.Text = f.Length > 0 ? "Nothing matches that filter." : "Waiting for the server to say something…";
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

    /// <summary>§ colour codes and ANSI SGR sequences. Hidden by CodeHider, honoured by LogColorizer.</summary>
    static readonly Regex CodeRx = new(@"\x1B\[[0-9;]*m|§[0-9a-fk-orA-FK-OR]|§#[0-9a-fA-F]{6}", RegexOptions.Compiled);

    /// <summary>Minecraft's 16 colours, with the darkest lifted so they read on the terminal.</summary>
    static readonly Dictionary<char, string> McColors = new()
    {
        ['0'] = "#5C6370", ['1'] = "#5C7CFF", ['2'] = "#2FBF4F", ['3'] = "#2CC4C4", ['4'] = "#E0474C",
        ['5'] = "#C35BD6", ['6'] = "#FFAA00", ['7'] = "#B0BAC5", ['8'] = "#7A828C", ['9'] = "#6E7BFF",
        ['a'] = "#55FF55", ['b'] = "#55FFFF", ['c'] = "#FF5555", ['d'] = "#FF6EFF", ['e'] = "#FFFF55",
        ['f'] = "#FFFFFF",
    };

    /// <summary>ANSI 30-37 / 90-97 in the same palette.</summary>
    static readonly string[] AnsiColors =
    {
        "#5C6370", "#E0474C", "#2FBF4F", "#FFAA00", "#5C7CFF", "#C35BD6", "#2CC4C4", "#B0BAC5",
        "#7A828C", "#FF5555", "#55FF55", "#FFFF55", "#6E7BFF", "#FF6EFF", "#55FFFF", "#FFFFFF",
    };

    /// <summary>Colour codes take no room: each one becomes a zero-width element.</summary>
    sealed class CodeHider : VisualLineElementGenerator
    {
        string Rest(int offset)
        {
            var end = CurrentContext.VisualLine.LastDocumentLine.EndOffset;
            return end > offset ? CurrentContext.Document.GetText(offset, end - offset) : "";
        }

        public override int GetFirstInterestedOffset(int startOffset)
        {
            var m = CodeRx.Match(Rest(startOffset));
            return m.Success ? startOffset + m.Index : -1;
        }

        public override VisualLineElement ConstructElement(int offset)
        {
            var m = CodeRx.Match(Rest(offset));
            return m.Success && m.Index == 0 ? new InlineObjectElement(m.Length, new Border { Width = 0, Height = 0 }) : null!;
        }
    }

    /// <summary>
    /// Colours the parts of a line rather than the whole of it: the timestamp and
    /// thread dim, the level by severity, plugin tags, player names (each player
    /// keeps one colour), chat, joins and leaves, commands, addresses, links and
    /// stack traces. § and ANSI codes from the server win over all of that.
    /// </summary>
    sealed class LogColorizer : DocumentColorizingTransformer
    {
        static readonly Regex Header = new(@"^\[[^\]]*?\d\d:\d\d:\d\d[^\]]*\](?:\s*\[[^\]]*\])*:?\s*", RegexOptions.Compiled);
        static readonly Regex Level = new(@"\b(INFO|WARN(?:ING)?|ERROR|SEVERE|FATAL|DEBUG|TRACE)\b", RegexOptions.Compiled);
        static readonly Regex Tag = new(@"^\[[^\]\s][^\]]{0,40}\]", RegexOptions.Compiled);
        static readonly Regex Chat = new(@"^(?:\[Not Secure\]\s*)?<([A-Za-z0-9_.]{2,17})>", RegexOptions.Compiled);
        static readonly Regex JoinLeft = new(@"^([A-Za-z0-9_.]{2,17}) (joined the game|left the game|lost connection.*)", RegexOptions.Compiled);
        static readonly Regex Issued = new(@"^([A-Za-z0-9_.]{2,17}) issued server command: (.*)$", RegexOptions.Compiled);
        static readonly Regex Login = new(@"^([A-Za-z0-9_.]{2,17})\[/([0-9a-fA-F.:]+)\] logged in", RegexOptions.Compiled);
        static readonly Regex Done = new(@"Done \([0-9.,]+s\)!.*", RegexOptions.Compiled);
        static readonly Regex Addr = new(@"/?\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b", RegexOptions.Compiled);
        static readonly Regex Url = new(@"https?://\S+", RegexOptions.Compiled);
        static readonly Regex Uuid = new(@"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", RegexOptions.Compiled);
        static readonly Regex Trace = new(@"^\s*(at |Caused by:|\.\.\. \d+ more|[a-z][\w$]*(\.[a-z][\w$]*)+\.[A-Z]\w*(Exception|Error)\b)", RegexOptions.Compiled);

        static readonly string[] NamePalette = { "#1BD96A", "#4F9CFF", "#C78AFF", "#FFA347", "#5EE6D0", "#FF7EB6", "#FFD65C" };

        static IBrush NameBrush(string name)
        {
            var h = 0;
            foreach (var c in name.ToLowerInvariant()) h = unchecked(h * 31 + c);
            return Ui.Hex(NamePalette[(int)((uint)h % NamePalette.Length)]);
        }

        static readonly IBrush Dim = Ui.Hex("#5F6873");
        static readonly IBrush ChatText = Ui.Hex("#E4E9EF");
        static readonly IBrush TraceText = Ui.Hex("#C97A86");
        static readonly IBrush JoinText = Ui.Hex("#7FE0A4");
        static readonly IBrush LeftText = Ui.Hex("#E8A27A");

        readonly List<(int start, int end, IBrush brush, bool bold)> _spans = new();

        void Span(int start, int end, IBrush brush, bool bold = false)
        {
            if (end > start) _spans.Add((start, end, brush, bold));
        }

        protected override void ColorizeLine(DocumentLine line)
        {
            if (line.Length == 0) return;
            var t = CurrentContext.Document.GetText(line);
            _spans.Clear();
            Plan(t);
            Codes(t);
            foreach (var (start, end, brush, bold) in _spans)
            {
                var a = Math.Max(0, start);
                var b = Math.Min(t.Length, end);
                if (b <= a) continue;
                ChangeLinePart(line.Offset + a, line.Offset + b, el =>
                {
                    el.TextRunProperties.SetForegroundBrush(brush);
                    if (bold)
                        el.TextRunProperties.SetTypeface(new Typeface(el.TextRunProperties.Typeface.FontFamily, FontStyle.Normal, FontWeight.Bold));
                });
            }
        }

        /// <summary>Structure first, broad to narrow, so later spans refine earlier ones.</summary>
        void Plan(string t)
        {
            // Lodestone's own lines and echoed commands.
            if (t.StartsWith(">>>", StringComparison.Ordinal)) { Span(0, 3, Ui.Brand, true); Span(3, t.Length, Ui.Hex("#9BE8BA")); return; }
            if (t.StartsWith("!!!", StringComparison.Ordinal)) { Span(0, 3, Ui.Red, true); Span(3, t.Length, Ui.Hex("#FF8FA6")); return; }
            if (t.StartsWith(">", StringComparison.Ordinal)) { Span(0, 1, Ui.Purple, true); Span(1, t.Length, Ui.Purple); return; }

            if (Trace.IsMatch(t)) { Span(0, t.Length, TraceText); return; }

            var head = Header.Match(t);
            var msgAt = head.Success ? head.Length : 0;
            var severity = "";
            if (head.Success)
            {
                Span(0, head.Length, Dim);
                var lv = Level.Match(head.Value);
                if (lv.Success)
                {
                    severity = lv.Value;
                    var brush = severity switch
                    {
                        "INFO" => Ui.Blue,
                        "WARN" or "WARNING" => Ui.Orange,
                        "ERROR" or "SEVERE" or "FATAL" => Ui.Red,
                        _ => Ui.Secondary,
                    };
                    Span(lv.Index, lv.Index + lv.Length, brush, severity != "INFO");
                }
            }

            var msg = t[msgAt..];
            if (severity is "ERROR" or "SEVERE" or "FATAL") { Span(msgAt, t.Length, Ui.Hex("#FF8FA6")); }
            else if (severity is "WARN" or "WARNING") { Span(msgAt, t.Length, Ui.Hex("#FFC48A")); }

            // [PluginName] or [Logger] at the start of the message.
            var tag = Tag.Match(msg);
            var bodyAt = msgAt;
            if (tag.Success)
            {
                Span(msgAt, msgAt + tag.Length, Ui.Purple);
                bodyAt = msgAt + tag.Length;
                while (bodyAt < t.Length && t[bodyAt] == ' ') bodyAt++;
            }
            var body = t[bodyAt..];

            Match m;
            if ((m = Chat.Match(body)).Success)
            {
                var g = m.Groups[1];
                Span(bodyAt, bodyAt + m.Length, Dim);
                Span(bodyAt + g.Index, bodyAt + g.Index + g.Length, NameBrush(g.Value), true);
                Span(bodyAt + m.Length, t.Length, ChatText);
            }
            else if ((m = JoinLeft.Match(body)).Success)
            {
                var g = m.Groups[1];
                Span(bodyAt, bodyAt + g.Length, NameBrush(g.Value), true);
                Span(bodyAt + m.Groups[2].Index, t.Length, m.Groups[2].Value.StartsWith("joined") ? JoinText : LeftText);
            }
            else if ((m = Issued.Match(body)).Success)
            {
                var g = m.Groups[1];
                Span(bodyAt, bodyAt + g.Length, NameBrush(g.Value), true);
                Span(bodyAt + m.Groups[2].Index, t.Length, Ui.Purple);
            }
            else if ((m = Login.Match(body)).Success)
            {
                var g = m.Groups[1];
                Span(bodyAt, bodyAt + g.Length, NameBrush(g.Value), true);
            }

            if ((m = Done.Match(t, msgAt)).Success) Span(m.Index, m.Index + m.Length, Ui.Brand, true);
            foreach (Match a in Addr.Matches(t, msgAt)) Span(a.Index, a.Index + a.Length, Ui.Orange);
            foreach (Match u in Uuid.Matches(t, msgAt)) Span(u.Index, u.Index + u.Length, Dim);
            foreach (Match u in Url.Matches(t, msgAt)) Span(u.Index, u.Index + u.Length, Ui.Blue);
        }

        /// <summary>§x and ANSI SGR codes colour everything after them, until reset.</summary>
        void Codes(string t)
        {
            IBrush? cur = null;
            var bold = false;
            var from = 0;
            void Close(int at)
            {
                if (cur != null || bold) Span(from, at, cur ?? Ui.Text, bold);
            }
            foreach (Match m in CodeRx.Matches(t))
            {
                Close(m.Index);
                var v = m.Value;
                if (v[0] == '§')
                {
                    var c = char.ToLowerInvariant(v[1]);
                    if (c == '#') { cur = Ui.Hex(v[1..]); }
                    else if (McColors.TryGetValue(c, out var hex)) { cur = Ui.Hex(hex); bold = false; }
                    else if (c == 'l') bold = true;
                    else if (c == 'r') { cur = null; bold = false; }
                }
                else
                {
                    var ps = v[2..^1].Split(';', StringSplitOptions.None);
                    for (var i = 0; i < ps.Length; i++)
                    {
                        if (!int.TryParse(ps[i].Length == 0 ? "0" : ps[i], out var p)) continue;
                        if (p == 0) { cur = null; bold = false; }
                        else if (p == 1) bold = true;
                        else if (p == 22) bold = false;
                        else if (p == 39) cur = null;
                        else if (p is >= 30 and <= 37) cur = Ui.Hex(AnsiColors[p - 30]);
                        else if (p is >= 90 and <= 97) cur = Ui.Hex(AnsiColors[p - 90 + 8]);
                        else if (p == 38 && i + 4 < ps.Length && ps[i + 1] == "2"
                                 && byte.TryParse(ps[i + 2], out var r) && byte.TryParse(ps[i + 3], out var g)
                                 && byte.TryParse(ps[i + 4], out var b))
                        {
                            cur = new SolidColorBrush(Color.FromRgb(r, g, b));
                            i += 4;
                        }
                        else if (p == 38 && i + 2 < ps.Length && ps[i + 1] == "5" && int.TryParse(ps[i + 2], out var n))
                        {
                            if (n < 16) cur = Ui.Hex(AnsiColors[n]);
                            i += 2;
                        }
                    }
                }
                from = m.Index + m.Length;
            }
            Close(t.Length);
        }
    }
}
