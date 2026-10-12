using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>Webhooks, each subscribed to its own events, and how chat is read out of the log.</summary>
public sealed class DiscordPage : Page
{
    public DiscordPage(MainWindow s) : base(s) { }

    public override string Key => "discord";
    public override string Group => "Automate";
    public override string Title => "Discord";
    public override string Icon => "discord";
    public override bool Advanced => true;

    List<JsonObject> _hooks = new();
    readonly HashSet<string> _open = new();

    TextBlock _stats = null!;
    StackPanel _list = null!;
    TextBlock _chatStat = null!;
    TextBox _chatPat = null!, _testLine = null!;
    StackPanel _testOut = null!, _hits = null!;

    protected override Control Build()
    {
        _stats = Ui.Hint("—");
        var head = Ui.Card(Ui.V(12,
            Ui.Split(Ui.V(3, Ui.H2("Discord webhooks"), _stats), Ui.Btn("Add webhook", () => _ = Add(), "plus", "blurple")),
            Ui.Hint("Every webhook picks its own events, so chat can land in one channel while crashes ping staff in another. " +
                    "Chat is relayed as the player, with their skin as the avatar. Get a URL from Discord under Server settings → " +
                    "Integrations → Webhooks. This is one-way - Discord messages do not come back into the game, which would need a bot.")));
        _list = Ui.V(12);

        _chatStat = Ui.Hint("—");
        _chatPat = Ui.Input("", "leave blank to auto-detect", mono: true);
        _testLine = Ui.Input("", "paste a chat line from your log to test it", mono: true).OnEnter(() => _ = TestLine());
        _testOut = Ui.V(8);
        _hits = Ui.V(8);
        var testRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        testRow.Children.Add(_testLine);
        var tb = Ui.Btn("Test", () => _ = TestLine(), null, "sm");
        tb.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(tb, 1);
        testRow.Children.Add(tb);
        var chat = Ui.Card(Ui.V(12,
            Ui.Split(Ui.V(3, Ui.H2("Chat detection"), _chatStat),
                Ui.H(8, Ui.Btn("Scan the log", () => _ = Sniff(), "search", "sm"), Ui.Btn("Save pattern", () => _ = SaveChat(), "save", "brand sm"))),
            Ui.Hint("Vanilla logs chat as <Name> message, but Paper logs it from an Async Chat Thread, and any chat plugin - or a " +
                    "Skript that cancels the chat event and re-broadcasts - rewrites the line completely. Lodestone reads both, strips " +
                    "colour codes and rank prefixes, and takes the last name before the separator. If your format is unusual, give it " +
                    "a regex with (?P<player>…) and (?P<message>…) groups."),
            Ui.Field("Custom pattern (optional)", _chatPat),
            testRow, _testOut, _hits));

        return Ui.Scroll(Ui.V(16, head, _list, chat), new Thickness(28, 16, 28, 28));
    }

    public override async Task OnShow()
    {
        await Load();
        LoadChatCfg();
    }

    // ================================================================ hooks

    async Task Load()
    {
        var j = await Ops.Get("/api/discord");
        if (j == null) return;
        _hooks = j.A("hooks").Items().Select(h => (JsonObject)h.DeepClone()).ToList();
        var st = j.N("stats");
        _stats.Text = st.S("sent", "0") + " sent · " + st.S("failed", "0") + " failed" +
                      (st.I("queued") > 0 ? " · " + st.S("queued") + " queued" : "") +
                      (st.S("last_error").Length > 0 ? " · last error: " + st.S("last_error") : "");
        Render();
    }

    void Render()
    {
        _list.Children.Clear();
        if (_hooks.Count == 0)
        {
            _list.Children.Add(Ui.Card(Ui.Empty("No webhooks yet. Add one and pick which events it should carry.", "webhook")));
            return;
        }
        for (var i = 0; i < _hooks.Count; i++) _list.Children.Add(HookCard(i));
    }

    string Meta(JsonObject h) =>
        Ui.Plural(h.A("events").Count, "event") +
        (h.S("url").Length > 0 ? "" : " · no URL yet") +
        (h.I("sent") > 0 ? " · " + h.S("sent") + " sent" : "") +
        (h.I("failed") > 0 ? " · " + h.S("failed") + " failed" : "") +
        (h.B("status_msg") ? " · live status" : "");

    Control HookCard(int i)
    {
        var h = _hooks[i];
        var id = h.S("id", "#" + i);
        var pill = Ui.Pill(h.B("enabled") ? "on" : "off", h.B("enabled") ? "on" : "");
        var meta = Ui.Hint(Meta(h));
        var chev = Icons.Make("chevron-down", 18, Ui.Secondary);
        var head = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto"), Background = Brushes.Transparent, Cursor = new Cursor(StandardCursorType.Hand) };
        var avatar = Ui.Rounded(Icons.Make("discord", 20, Ui.Hex("#FFFFFF")), 40, 12, Ui.Res("MrBlurple"));
        head.Children.Add(avatar);
        var mid = Ui.V(3, Ui.H(10, Ui.H3(h.S("name", "webhook")), pill), meta);
        mid.Margin = new Thickness(14, 0, 12, 0);
        mid.VerticalAlignment = VerticalAlignment.Center;
        Grid.SetColumn(mid, 1);
        head.Children.Add(mid);
        Grid.SetColumn(chev, 2);
        head.Children.Add(chev);

        var body = Ui.V(0);
        body.Margin = new Thickness(0, 18, 0, 0);
        body.IsVisible = _open.Contains(id);
        chev.RenderTransform = body.IsVisible ? new RotateTransform(180) : null;
        head.PointerPressed += (_, _) =>
        {
            body.IsVisible = !body.IsVisible;
            if (body.IsVisible) _open.Add(id);
            else _open.Remove(id);
            chev.RenderTransform = body.IsVisible ? new RotateTransform(180) : null;
        };

        TextBox Tx(string k, string? ph = null, bool mono = false, bool pw = false)
        {
            var t = Ui.Input(h.S(k), ph, pw, mono);
            t.TextChanged += (_, _) => h[k] = t.Text ?? "";
            return t;
        }
        ToggleButton Sw(string k, string label, bool on, string? tip = null)
        {
            var s = Ui.Switch(label, on, v =>
            {
                h[k] = v;
                if (k == "enabled") Ui.SetPill(pill, v ? "on" : "off", v ? "on" : "");
            });
            if (tip != null) ToolTip.SetTip(s, tip);
            return s;
        }
        var style = Ui.Select(new[] { ("player", "As the player (skin avatar)"), ("compact", "Batched lines (kinder to rate limits)") },
            h.S("chat_style") == "compact" ? "compact" : "player", v => h["chat_style"] = v);

        body.Children.Add(Ui.Cols(16, Ui.Field("Name", Tx("name")), Ui.Field("Post as", Tx("username"))));
        body.Children.Add(Ui.Field("Webhook URL", Tx("url", "https://discord.com/api/webhooks/…", true)));
        body.Children.Add(Ui.Cols(16, Ui.Field("Avatar URL (optional)", Tx("avatar")),
            Ui.Field("Ping on crashes/errors", Tx("mention", "<@&roleid> or @here"))));
        body.Children.Add(Ui.Cols(16, Ui.Field("Thread ID (optional)", Tx("thread_id", "post into a thread instead of the channel")),
            Ui.Field("Chat style", style)));
        var switches = new WrapPanel();
        foreach (var sw in new[]
                 {
                     Sw("enabled", "Enabled", h.B("enabled")),
                     Sw("embeds", "Rich embeds", h.B("embeds")),
                     Sw("status_msg", "Live status message", h.B("status_msg"), "Keeps one message updated instead of posting again"),
                     Sw("attach_log", "Attach log on crash", !h.IsFalse("attach_log"), "Attach the tail of the console to crash reports"),
                 })
        {
            sw.Margin = new Thickness(0, 0, 22, 10);
            switches.Children.Add(sw);
        }
        body.Children.Add(switches);
        body.Children.Add(Ui.H(8,
            Ui.Btn("Send test", () => _ = TestHook(i), "send", "sm"),
            Ui.Btn("Test an event", () => TestEvent(i), "zap", "sm"),
            Ui.Btn("Save", () => _ = Save(false), "save", "brand sm"),
            Ui.Btn("Remove", () => Remove(i), "trash", "soft-red sm")).Also(r => r.Margin = new Thickness(0, 4, 0, 18)));

        body.Children.Add(Ui.Eyebrow("Events").Also(t => t.Margin = new Thickness(0, 0, 0, 8)));
        var grid = new UniformGrid { Columns = 3 };
        grid.SizeChanged += (_, e) =>
        {
            var c = Math.Max(1, (int)(e.NewSize.Width / 240));
            if (c != grid.Columns) grid.Columns = c;
        };
        var evs = h.Strings("events");
        foreach (var e in S.Events.Items())
        {
            if (e is not JsonArray ea || ea.Count < 3) continue;
            var key = J.Str(ea[0]);
            var tile = Ui.Tile(J.Str(ea[1]), J.Str(ea[2]), evs.Contains(key));
            tile.Margin = new Thickness(0, 0, 8, 8);
            tile.IsCheckedChanged += async (_, _) =>
            {
                var list = h.Strings("events");
                if (tile.IsChecked == true && !list.Contains(key)) list.Add(key);
                if (tile.IsChecked != true) list.Remove(key);
                h["events"] = new JsonArray(list.Select(x => (JsonNode?)x).ToArray());
                meta.Text = Meta(h);
                await Save(true, reload: false);
            };
            grid.Children.Add(tile);
        }
        body.Children.Add(grid);

        return Ui.Card(Ui.V(0, head, body), 18);
    }

    async Task Save(bool quiet, bool reload = true)
    {
        var arr = new JsonArray();
        foreach (var h in _hooks) arr.Add(h.DeepClone());
        var j = await Ops.Act("/api/discord", new JsonObject { ["hooks"] = arr }, quiet ? null : "Webhooks saved.");
        if (j != null && reload) await Load();
    }

    async Task Add()
    {
        try
        {
            await Api.Post("/api/discord/add", new { });
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
            return;
        }
        await Load();
        if (_hooks.Count > 0)
        {
            _open.Add(_hooks[^1].S("id", "#" + (_hooks.Count - 1)));
            Render();
        }
    }

    async void Remove(int i)
    {
        if (!await Dialogs.Confirm("Remove this webhook?", "Discord keeps the webhook itself - this just stops Lodestone using it.", "Remove"))
            return;
        _hooks.RemoveAt(i);
        await Save(true);
        Toasts.Good("Removed.");
    }

    async Task TestHook(int i)
    {
        await Save(true, reload: false);
        try
        {
            var j = await Api.Post("/api/discord/test", new { id = _hooks[i].S("id") });
            if (j.B("ok")) Toasts.Good("Test posted to Discord.");
            else Toasts.Bad(j.S("error"));
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
        await Load();
    }

    async void TestEvent(int i)
    {
        var evs = _hooks[i].Strings("events");
        if (evs.Count == 0)
        {
            Toasts.Bad("Give it some events first.");
            return;
        }
        var opts = S.Events.Items().OfType<JsonArray>().Where(e => evs.Contains(J.Str(e[0])))
            .Select(e => (J.Str(e[0]), J.Str(e[1])));
        var k = Ui.Select(opts, evs[0]);
        var t = Ui.Input("Test event");
        var d = Ui.Input("Fired from the panel.");
        var body = Ui.V(0, Ui.Field("Event", k), Ui.Field("Title", t), Ui.Field("Description", d),
            Ui.Hint("This goes to every webhook subscribed to that event, exactly as the real one would."));
        if (await Dialogs.Show("Fire a test event", body, ("Cancel", "", "ghost"), ("Fire", "ok", "brand")) != "ok") return;
        await Ops.Act("/api/discord/fire", new { @event = Ui.Val(k), title = t.Text, description = d.Text }, "Fired.");
        await Task.Delay(1200);
        await Load();
    }

    // ================================================================ chat

    async void LoadChatCfg()
    {
        var st = S.State.N("chat");
        _chatStat.Text = st.I("seen") > 0
            ? st.S("seen") + " message(s) picked up" + (st.S("last").Length > 0 ? " · last: " + st.S("last") : "")
            : "nothing seen yet this session";
        try
        {
            var j = await Api.Get("/api/servers/get?id=" + Api.Q(S.State.N("server").S("id")));
            _chatPat.Text = j.N("server").N("chat").S("pattern");
        }
        catch (Exception)
        {
            // leave the box as it was
        }
    }

    async Task SaveChat()
    {
        if (await Ops.Act("/api/chat/save", new { pattern = _chatPat.Text ?? "", enabled = true }, "Saved.") != null)
            await Sniff();
    }

    async Task Sniff()
    {
        _hits.Children.Clear();
        _hits.Children.Add(Ui.Empty("scanning…"));
        try
        {
            var j = await Api.Get("/api/chat/sniff");
            _hits.Children.Clear();
            var threads = j.A("threads").Items().OfType<JsonArray>().Select(t => J.Str(t[0])).ToList();
            _hits.Children.Add(Ui.Hint("Scanned " + j.S("scanned", "0") + " line(s). Found " + j.S("count", "0") + " chat message(s)" +
                                       (j.B("custom") ? " using your custom pattern" : " with auto-detection") + "." +
                                       (threads.Count > 0 ? " Chat arrives on: " + string.Join(", ", threads) + "." : "")));
            var hits = j.A("hits").Items().ToList();
            if (hits.Count > 0)
            {
                foreach (var x in hits.TakeLast(12).Reverse())
                    _hits.Children.Add(Ui.Row(Ui.Lead(Images.Slot(Images.Head(x.S("player")), 32, 8),
                        Ui.TitleSub(x.S("player") + " - " + x.S("message"), x.S("line")))));
            }
            else
            {
                _hits.Children.Add(Ui.Empty("No chat found in the recent log. If people have talked since the server started, " +
                                            "paste one of those lines into the test box above.", "message"));
            }
            var un = j.Strings("unmatched");
            if (un.Count > 0)
            {
                _hits.Children.Add(Ui.Hint("These came off the chat thread but did not parse - they may need a custom pattern:"));
                _hits.Children.Add(Ui.Code(string.Join("\n", un)));
            }
        }
        catch (Exception e)
        {
            _hits.Children.Clear();
            _hits.Children.Add(Ui.Empty(e.Message, "alert"));
        }
    }

    async Task TestLine()
    {
        var line = _testLine.Text ?? "";
        if (line.Trim().Length == 0) return;
        try
        {
            var j = await Api.Post("/api/chat/test", new { line });
            _testOut.Children.Clear();
            var matched = j.B("matched");
            var title = matched ? "Parsed as " + j.S("player") + ": " + j.S("message") : "Not recognised as chat";
            var sub = j.B("auto_matched") && !matched
                ? "auto-detection would have read it as " + j.S("auto_player") + ": " + j.S("auto_message") + " - clear your custom pattern to use that"
                : matched ? "this line will be relayed" : "give it a custom pattern above, or check it really is a chat line";
            var row = Ui.Row(Ui.Lead(Icons.Make(matched ? "circle-check" : "circle-x", 20, matched ? Ui.Brand : Ui.Red),
                Ui.TitleSub(title, sub)));
            if (matched) row.Classes.Add("active");
            _testOut.Children.Add(row);
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
    }
}
