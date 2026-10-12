using System.Reflection;
using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

public sealed class SettingsPage : Page
{
    public SettingsPage(MainWindow s) : base(s) { }

    public override string Key => "settings";
    public override string Title => "Settings";
    public override string Icon => "settings";
    public override bool ServerScoped => false;

    StackPanel _body = null!;

    protected override Control Build()
    {
        _body = Ui.V(20);
        return Ui.Scroll(_body, new Thickness(28, 24, 28, 28));
    }

    public override async Task OnShow()
    {
        var me = await Ops.Get("/api/me") ?? new JsonObject();
        _body.Children.Clear();
        _body.Children.Add(Ui.V(4, Ui.H1("Settings"), Ui.Hint("The engine's own settings, then this window's.")));

        // ---- updates
        _updHost = new ContentControl();
        _body.Children.Add(_updHost);
        _ = LoadUpdates();

        // ---- engine
        var host = Ui.Select(new[] { ("127.0.0.1", "127.0.0.1 - this machine only"), ("0.0.0.0", "0.0.0.0 - anyone on the LAN") },
            me.S("host", "127.0.0.1"));
        var port = Ui.Input(me.S("port", "8744"), mono: true);
        var engineCard = Ui.Card("Engine", "The Lodestone engine this window talks to",
            Ui.Btn("Save", async () =>
            {
                int.TryParse(port.Text, out var p);
                await Ops.Act("/api/settings", new { host = Ui.Val(host), port = p }, "Saved - restart Lodestone to apply.");
            }, "save", "brand sm"),
            Ui.Cols(32,
                Ui.V(0,
                    Ui.Field("Engine address", host,
                        "The engine's API has no login. On 0.0.0.0 every device on your network can control your servers. Takes effect " +
                        "when Lodestone restarts."),
                    Ui.Field("Port", port, "Restart Lodestone to move it.")),
                Ui.V(0,
                    Ui.KRow("Reachable at", me.S("url")),
                    Ui.KRow("LAN address", me.S("lan_ip") + ":" + me.S("port")),
                    Ui.KRow("Config file", me.S("config")),
                    Ui.KRow("Backups kept in", me.S("backups")),
                    Ui.KRow("Engine", S.Engine.How),
                    S.Engine.Owned ? Ui.KRow("Engine log", S.Engine.LogPath) : null,
                    Ui.KRow("paramiko", me.B("paramiko") ? "installed" : "missing", me.B("paramiko") ? Ui.Brand : Ui.Orange),
                    me.B("paramiko") ? null : Ui.Btn("Install paramiko", () => _ = InstallParamiko(), "download", "sm")
                        .Also(b => { b.Margin = new Thickness(0, 10, 0, 0); b.HorizontalAlignment = HorizontalAlignment.Right; }))));
        _body.Children.Add(engineCard);

        // ---- window
        var mode = Ui.Select(new[] { ("simple", "Simple - the essentials"), ("advanced", "Advanced - everything") }, S.Mode,
            v => _ = S.SetMode(v));
        var mono = SizeSlider(10, 28, S.Prefs.Mono, v => { S.Prefs.Mono = v; S.ApplySizes(); }, "px code");
        var ui = SizeSlider(12, 22, S.Prefs.Ui, v => { S.Prefs.Ui = v; S.ApplySizes(); }, "px interface");
        var window = Ui.Card("This window", "Kept on this PC, per user", null, Ui.Cols(32,
            Ui.Field("How much to show", mode,
                "Simple hides log search, plugins, worlds, automation, the assistant, Discord and the API. Nothing is lost - the tabs " +
                "come straight back."),
            Ui.V(0,
                Ui.Field("Console and editor text", mono, "Also Ctrl +/- while editing a file."),
                Ui.Field("Interface size", ui),
                Ui.SwitchHint(Ui.Switch("Play the intro when Lodestone opens", S.Prefs.Intro, v =>
                {
                    S.Prefs.Intro = v;
                    S.Prefs.Save();
                }), "About five seconds while the engine connects. Click it to skip once it is ready."))));
        _body.Children.Add(window);

        // ---- sounds
        var p = S.Prefs;
        ToggleButton Cat(string label, bool on, Action<bool> set) => Ui.Switch(label, on, v =>
        {
            set(v);
            p.Save();
        });
        var master = Ui.Switch("Sound effects", p.SoundOn, v => Sounds.SetOn(v));
        Sounds.Changed += () => master.IsChecked = Sounds.On;
        var volText = Ui.T(p.SoundVolume + "%", null, 13, Ui.Secondary, FontWeight.SemiBold);
        var vol = new Slider { Minimum = 0, Maximum = 100, Value = p.SoundVolume, Width = 260, TickFrequency = 5, IsSnapToTickEnabled = true };
        var settle = new Avalonia.Threading.DispatcherTimer { Interval = TimeSpan.FromMilliseconds(250) };
        settle.Tick += (_, _) =>
        {
            settle.Stop();
            p.Save();
            Sounds.Play("success", true);
        };
        vol.ValueChanged += (_, e) =>
        {
            p.SoundVolume = (int)e.NewValue;
            volText.Text = p.SoundVolume + "%";
            settle.Stop();
            settle.Start();
        };
        var previews = new WrapPanel();
        foreach (var (key, label) in Sounds.Previews)
        {
            var k = key;
            var b = Ui.Btn(label, null, "play", "xs");
            // the global click sound would cut the preview off, so this one plays after it
            b.Click += (_, _) => Avalonia.Threading.Dispatcher.UIThread.Post(() => Sounds.Play(k, true),
                Avalonia.Threading.DispatcherPriority.Background);
            b.Margin = new Thickness(0, 0, 6, 6);
            previews.Children.Add(b);
        }
        _body.Children.Add(Ui.Card("Sounds", "Little 8-bit effects, made by the app itself", master, Ui.Cols(32,
            Ui.V(0,
                Ui.Field("Volume", Ui.H(12, vol, volText)),
                Ui.SwitchHint(Cat("Buttons and tabs", p.SoundUi, v => p.SoundUi = v), "A soft tick on every click, a pop when a dialog opens."),
                Ui.SwitchHint(Cat("Notifications", p.SoundNotify, v => p.SoundNotify = v), "A chime when something worked, a buzz when it failed."),
                Ui.SwitchHint(Cat("Server start and stop", p.SoundServer, v => p.SoundServer = v),
                    "A rising tune when the server comes up, a falling one when it stops, three beeps on an error."),
                Ui.SwitchHint(Cat("Players joining and leaving", p.SoundPlayers, v => p.SoundPlayers = v), null),
                Ui.SwitchHint(Cat("Chat messages", p.SoundChat, v => p.SoundChat = v), "A quiet pop, at most about once a second.")),
            Ui.V(0, Ui.Field("Try them", previews, "The speaker key in the top bar mutes everything at once.")))));

        // ---- quit
        _body.Children.Add(Ui.Card(Ui.Split(
            Ui.V(4, Ui.H2("Quit Lodestone"),
                Ui.Hint("Stops the engine and releases its port. Any Minecraft server it is running is stopped first, and port " +
                        "forwards Lodestone created are removed. Closing the window does the same when this window started the engine.")),
            Ui.Btn("Quit Lodestone", () => _ = S.QuitAll(), "power", "danger"))));

        // ---- about
        var ver = Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "1.1.0";
        _body.Children.Add(Ui.Card(Ui.H(18, Logo.Make(56),
            Ui.V(6,
                Ui.T("Lodestone " + me.S("version", "") + " · desktop " + ver, null, 16, Ui.Contrast, FontWeight.Bold),
                Ui.Hint("One panel for local and remote Minecraft servers. This window is C# and Avalonia; the engine underneath is the " +
                        "same lodestone.py that does the work - local processes, SFTP, RCON, Discord, hosting and the REST API. " +
                        "The engine still serves the old browser page at " + me.S("url") + " for anyone who wants it.")
                    .Also(h => h.MaxWidth = 760)))));
    }

    // ================================================================ updates

    ContentControl _updHost = null!;
    bool _updBusy;

    async Task LoadUpdates()
    {
        try { RenderUpdates(await Api.Get("/api/update")); }
        catch (Exception e) { _updHost.Content = Ui.Card("Updates", null, null, Ui.Hint(e.Message)); }
    }

    async Task UpdateDo(string path, string busy, object? body = null)
    {
        if (_updBusy) return;
        _updBusy = true;
        try
        {
            Toasts.Show(busy);
            var j = await Ops.Act(path, body, "");
            if (j != null) RenderUpdates(j);
            else await LoadUpdates();
        }
        finally
        {
            _updBusy = false;
        }
    }

    static string When(string iso)
    {
        if (!DateTimeOffset.TryParse(iso, out var t)) return iso;
        var ago = DateTimeOffset.Now - t;
        return ago.TotalMinutes < 2 ? "just now" : ago.TotalHours < 1 ? (int)ago.TotalMinutes + " min ago"
            : ago.TotalDays < 1 ? (int)ago.TotalHours + " h ago" : ago.TotalDays < 30 ? (int)ago.TotalDays + " days ago"
            : t.LocalDateTime.ToString("d MMM yyyy");
    }

    void RenderUpdates(JsonNode u)
    {
        var st = u.N("settings");
        var info = u.N("info");
        var state = u.S("state");
        var repoText = st.S("repo") + "  ·  " + st.S("branch") + (st.S("path").Length > 0 ? "  ·  " + st.S("path") + "/" : "");

        // ---- left: where it stands
        var left = Ui.V(12);
        (string icon, IBrush col, string head, string sub) look = state switch
        {
            "ready" when u.N("applied").B("rolled_back") => ("restart", Ui.Brand, "The previous version is back",
                "Restart Lodestone to use it."),
            "ready" => ("circle-check", Ui.Brand, "Update installed - restart to use it",
                u.N("applied").S("sha").Length > 0 ? "Now at " + u.N("applied").S("sha")[..7] + " · " + u.N("applied").S("title") : ""),
            "available" => ("download", Ui.Orange, info.N("behind") == null ? "An update is available"
                : Ui.Plural(info.I("behind"), "new change") + " available", "Latest: " + info.N("latest").S("title")),
            "current" => ("circle-check", Ui.Brand, "Up to date", info.N("current").S("title")),
            "checking" => ("refresh", Ui.Blue, "Checking GitHub…", ""),
            "installing" => ("download", Ui.Blue, "Installing the update… " + u.S("pct") + "%", ""),
            "error" => ("circle-x", Ui.Red, "The last check failed", u.S("error")),
            _ => ("info", Ui.Secondary, "Not checked yet", st.B("enabled") ? "Lodestone checks a little after it starts." : ""),
        };
        if (!u.B("supported")) look = ("alert", Ui.Orange, "Updates are not available here", u.S("reason"));
        left.Children.Add(Ui.Lead(Ui.Rounded(Icons.Make(look.icon, 22, look.col), 44, 12),
            Ui.V(2, Ui.T(look.head, null, 16, look.col == Ui.Secondary ? Ui.Contrast : look.col, FontWeight.Bold),
                look.sub.Length > 0 ? Ui.T(look.sub, null, 12.5, Ui.Secondary, wrap: true) : null)));

        var acts = new WrapPanel();
        void Act(Control c)
        {
            c.Margin = new Thickness(0, 0, 8, 8);
            acts.Children.Add(c);
        }
        if (u.B("supported"))
        {
            if (state == "ready")
                Act(Ui.Btn("Restart now", () => _ = S.RestartForUpdate(), "restart", "brand"));
            if (state == "available")
                Act(Ui.Btn("Install now", () => _ = UpdateDo("/api/update/install", "Downloading the update…"), "download", "brand"));
            Act(Ui.Btn("Check now", () => _ = UpdateDo("/api/update/check", "Asking GitHub…"), "refresh", state is "ready" or "available" ? "" : "brand"));
            if (u.B("can_rollback") && state != "installing")
                Act(Ui.Btn("Go back to the previous version", async () =>
                {
                    if (await Dialogs.Confirm("Go back to the previous version?",
                            "The files the last update replaced are put back. Restart Lodestone afterwards to use them.", "Go back", ""))
                        await UpdateDo("/api/update/rollback", "Putting the previous version back…");
                }, "arrow-left", "ghost"));
        }
        left.Children.Add(acts);
        if (u.I("running_servers") > 0 && state == "ready")
            left.Children.Add(Ui.Hint("A server is running - restarting stops it cleanly first (you will be asked)."));

        var commits = info.A("commits").Items().ToList();
        if (commits.Count > 0)
        {
            left.Children.Add(Ui.Eyebrow("What's new"));
            foreach (var c in commits.Take(12))
                left.Children.Add(Ui.Row(Ui.Lead(Ui.Rounded(Icons.Make("zap", 14, Ui.Orange), 28, 8),
                    Ui.V(2, Ui.T(c.S("title"), "h3").Also(t => t.TextWrapping = TextWrapping.Wrap),
                        Ui.T(c.S("short") + "  ·  " + c.S("author") + "  ·  " + When(c.S("date")), null, 12.5, Ui.Secondary)))));
            if (commits.Count > 12) left.Children.Add(Ui.Hint("…and " + (commits.Count - 12) + " more."));
        }
        var inst = u.N("installed");
        left.Children.Add(Ui.V(0,
            Ui.KRow("Installed version", inst.S("sha").Length > 0 ? inst.S("sha")[..7] + " · " + When(inst.S("date"))
                : info.S("mode") == "git" ? "git checkout · " + info.N("current").S("short") : "not stamped yet - the first update stamps it"),
            Ui.KRow("Folder", u.S("root")),
            Ui.KRow("Signed in to GitHub with", info.S("auth", "—")),
            Ui.KRow("Last checked", u.L("checked_at") > 0
                ? When(DateTimeOffset.FromUnixTimeSeconds(u.L("checked_at")).ToString("o")) : "never")));

        // ---- right: how
        var auto = Ui.Switch("Install updates by itself", st.B("auto_install"));
        var autoRestart = Ui.Switch("Restart by itself when no server is running", st.B("auto_restart"));
        var hours = Ui.Select(new[] { ("1", "every hour"), ("3", "every 3 hours"), ("6", "every 6 hours"), ("12", "every 12 hours"),
            ("24", "once a day"), ("168", "once a week") }, st.S("hours", "6"));
        hours.HorizontalAlignment = HorizontalAlignment.Stretch;
        var repo = Ui.Input(st.S("repo"), "owner/name", mono: true);
        var branch = Ui.Input(st.S("branch"), "main", mono: true);
        var path = Ui.Input(st.S("path"), "Lodestone", mono: true);
        var gitLogin = Ui.Switch("Use the GitHub login git already has", st.B("use_git_login"));
        var token = Ui.Input("", st.B("has_token") ? "saved - type to replace, or - to remove" : "only needed for a private repo", true);
        var right = Ui.V(0,
            Ui.SwitchHint(auto, "New versions are downloaded, checked and put in place in the background. Nothing changes " +
                                "until Lodestone restarts."),
            Ui.SwitchHint(autoRestart, "Only ever while no Minecraft server is running, so nobody is kicked."),
            Ui.Field("Check for updates", hours),
            Ui.Field("Repository", repo),
            Ui.Cols(12, Ui.Field("Branch", branch), Ui.Field("Folder", path)),
            Ui.SwitchHint(gitLogin, "The repository is private, so GitHub wants a login. With this on, Lodestone borrows the one " +
                                    "git (or the GitHub CLI) uses on this PC - nothing is stored."),
            Ui.Field("GitHub token", token, "Optional. A fine-grained token with Contents: read-only on this repository. " +
                                            "Stored in ~/.lodestone.json and never shown again."),
            Ui.Btn("Save", () => _ = UpdateDo("/api/update/settings", "Saving…", new JsonObject
            {
                ["enabled"] = true,
                ["auto_install"] = auto.IsChecked == true, ["auto_restart"] = autoRestart.IsChecked == true,
                ["hours"] = Ui.Val(hours), ["repo"] = repo.Text ?? "", ["branch"] = branch.Text ?? "", ["path"] = path.Text ?? "",
                ["use_git_login"] = gitLogin.IsChecked == true, ["token"] = token.Text ?? "",
            }), "save", "sm").Also(b => b.HorizontalAlignment = HorizontalAlignment.Left));

        var enabled = Ui.Switch("Check by itself", st.B("enabled"), v =>
            _ = UpdateDo("/api/update/settings", v ? "Lodestone will check for updates." : "Automatic checks are off.",
                new { enabled = v }));
        _updHost.Content = Ui.Card("Updates", "From github.com/" + repoText, enabled, Ui.Cols(32, left, right));

        // keep the progress moving while something is happening
        if (state is "checking" or "installing")
            DispatcherTimer.RunOnce(() => { if (Showing) _ = LoadUpdates(); }, TimeSpan.FromSeconds(1));
    }

    static Control SizeSlider(int min, int max, int value, Action<int> set, string unit)
    {
        var label = Ui.Pill(value + unit, "", false);
        var sl = new Slider { Minimum = min, Maximum = max, Value = value, TickFrequency = 1, IsSnapToTickEnabled = true };
        sl.PropertyChanged += (_, e) =>
        {
            if (e.Property != RangeBase.ValueProperty) return;
            var v = (int)Math.Round(sl.Value);
            Ui.SetPill(label, v + unit, "", false);
            set(v);
        };
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        g.Children.Add(sl);
        label.Margin = new Thickness(12, 0, 0, 0);
        Grid.SetColumn(label, 1);
        g.Children.Add(label);
        return g;
    }

    static async Task InstallParamiko()
    {
        Toasts.Show("Installing paramiko…");
        var j = await Ops.Act("/api/install", null, null);
        if (j != null) Toasts.Show(j.S("msg"), j.B("ok") ? "good" : "bad");
    }
}
