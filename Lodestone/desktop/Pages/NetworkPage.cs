using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// Who can reach the server: local, LAN, internet (a router port forward) or a
/// tunnel. The work - UPnP, NAT-PMP, PCP, firewall, reachability tests - is
/// the engine's networking package; this draws its state.
/// </summary>
public sealed class NetworkPage : Page
{
    public NetworkPage(MainWindow s) : base(s) { }

    public override string Key => "network";
    public override string Title => "Network";
    public override string Icon => "globe";
    public override bool ServerScoped => false;

    static readonly (string key, string name, string icon, string why)[] Modes =
    {
        ("local", "Local only", "drive", "Only this PC can connect. The server binds to 127.0.0.1."),
        ("lan", "LAN", "wifi", "Anyone on your home network can connect. No router changes."),
        ("internet", "Internet", "globe", "Friends outside your network can connect. Asks your router to forward the Minecraft port, " +
                                          "then checks from outside whether it worked."),
        ("tunnel", "Tunnel", "webhook", "For when port forwarding cannot work - CGNAT, or a router you have no password for. Runs a " +
                                        "playit.gg or ngrok agent that dials out, so nothing has to be opened on any router."),
    };

    JsonNode? _net;
    IDisposable? _poll;
    bool _wasBusy;

    Border _phase = null!;
    TextBlock _stamp = null!;
    StackPanel _modes = null!, _dash = null!, _fix = null!, _steps = null!, _warn = null!, _settings = null!;
    Border _fixCard = null!, _stepsCard = null!, _warnCard = null!;

    // settings controls
    TextBox _port = null!, _extPort = null!, _agent = null!, _ngrok = null!;
    ToggleButton _upnp = null!, _natpmp = null!, _pcp = null!, _fw = null!, _pubIp = null!, _ext = null!, _auto = null!;
    ComboBox _provider = null!;
    Control _playitRow = null!, _ngrokRow = null!;

    protected override Control Build()
    {
        _phase = Ui.Pill("…");
        _stamp = Ui.Hint("—");
        _modes = Ui.V(10);
        _dash = Ui.V(0);
        _fix = Ui.V(8);
        _steps = Ui.V(0);
        _warn = Ui.V(12);
        _settings = Ui.V(0);

        var head = Ui.V(4,
            Ui.Split(Ui.H(12, Ui.H1("Network"), _phase),
                Ui.H(8,
                    Ui.Btn("Auto fix", AutoFix, "zap", "brand", "Fix everything that can be fixed from this PC, then say what cannot"),
                    Ui.Btn("Check router", () => _ = Probe(), "wifi", "", "Which port-mapping protocols your router supports"),
                    Ui.Btn("Test connection", () => _ = Test(), "activity"),
                    Ui.IconBtn("refresh", "Refresh", () => _ = Load(), "", 36))),
            Ui.Hint("Only the Minecraft port is ever published. RCON, SFTP, SSH and this panel stay on your network."));

        var modesCard = Ui.Card("Hosting", "How people reach the server on screen", null, _modes);
        _fixCard = Ui.Card("Auto fix", null, Ui.Btn("Hide", () => _fixCard.IsVisible = false, null, "ghost sm"), _fix);
        _fixCard.IsVisible = false;
        var dashCard = Ui.Card("Minecraft server", null, _stamp, _dash);
        _stepsCard = Ui.Card("What happened", null, Ui.Btn("Hide", () => _stepsCard.IsVisible = false, null, "ghost sm"), _steps);
        _stepsCard.IsVisible = false;
        _warnCard = Ui.Card("Before you share the address", null,
            Ui.Btn("Deep check", () => _ = Audit(true), "search", "sm", "Also read the router's forwarding table"), _warn);
        _warnCard.IsVisible = false;
        var settingsCard = Ui.Card("Settings", null, Ui.Btn("Save", () => _ = SaveSettings(), "save", "brand sm"), _settings);

        var cols = new Grid { ColumnDefinitions = new ColumnDefinitions("*,16,*") };
        var left = Ui.V(16, modesCard, _fixCard, _stepsCard);
        var right = Ui.V(16, dashCard, _warnCard);
        cols.Children.Add(left);
        Grid.SetColumn(right, 2);
        cols.Children.Add(right);
        return Ui.Scroll(Ui.V(20, head, cols, settingsCard), new Thickness(28, 24, 28, 28));
    }

    public override Task OnShow() => Load();

    public override void OnHide() => StopPoll();

    public override void OnStatus(JsonNode s)
    {
        if (!Built || !Showing || _net == null) return;
        // something finished in the background - pick up the full picture once
        var busy = s.N("hosting").B("busy");
        if (_wasBusy && !busy) _ = Load();
        _wasBusy = busy;
    }

    void StartPoll()
    {
        if (_poll != null) return;
        _poll = DispatcherTimer.Run(() =>
        {
            if (!Showing)
            {
                _poll = null;
                return false;
            }
            _ = Load();
            return true;
        }, TimeSpan.FromMilliseconds(1500));
    }

    void StopPoll()
    {
        _poll?.Dispose();
        _poll = null;
    }

    async Task Load()
    {
        JsonNode j;
        try { j = await Api.Get("/api/hosting"); }
        catch (Exception e)
        {
            _modes.Children.Clear();
            _modes.Children.Add(Ui.Hint(e.Message));
            return;
        }
        _net = j;
        _wasBusy = j.B("busy");
        if (j.IsFalse("available"))
        {
            _modes.Children.Clear();
            _modes.Children.Add(Ui.Hint(j.S("error", "Networking support is not available.") +
                                        "\n\nLocal and LAN hosting still work - this only affects automatic router configuration and the reachability test."));
            _dash.Children.Clear();
            return;
        }
        RenderModes(j);
        RenderDash(j);
        RenderSteps(j);
        RenderFix(j);
        RenderSettings(j);
        var label = j.B("busy") ? "working…"
            : j.S("mode") == "internet" ? (j.B("online") ? "online" : j.S("connectivity_label").ToLowerInvariant())
            : j.S("mode_label").ToLowerInvariant();
        Ui.SetPill(_phase, label, j.B("busy") ? "warn" : j.B("online") ? "on" : j.S("phase") == "failed" ? "bad" : "");
        var at = j.D("checked_at");
        _stamp.Text = at > 0 ? "checked " + DateTimeOffset.FromUnixTimeMilliseconds((long)(at * 1000)).ToLocalTime().ToString("HH:mm:ss") : "not checked yet";
        _ = Audit(false);
        if (j.B("busy")) StartPoll();
        else StopPoll();
    }

    // ================================================================ modes

    void RenderModes(JsonNode j)
    {
        _modes.Children.Clear();
        if (!j.B("can_host"))
        {
            _modes.Children.Add(Ui.Hint(j.S("why_not")));
            return;
        }
        foreach (var (key, name, icon, why) in Modes)
        {
            var on = j.S("mode") == key;
            Control action = on
                ? (key is "internet" or "tunnel" ? Ui.Btn("Turn off", Disable, "power", "soft-red sm") : Ui.Pill("active", "on"))
                : Ui.Btn("Use " + name.ToLowerInvariant(), () => _ = SetMode(key), null, key == "internet" ? "brand sm" : "sm");
            var row = Ui.Row(Ui.Lead(Ui.Rounded(Icons.Make(icon, 20, on ? Ui.Brand : Ui.Secondary), 40, 12),
                Ui.TitleSub(name + (on ? "  ·  current" : ""), why)), action);
            if (on) row.Classes.Add("active");
            _modes.Children.Add(row);
        }
        if (j.S("property_error").Length > 0)
        {
            _modes.Children.Add(Ui.T(j.S("property_error"), null, 13, Ui.Orange, wrap: true));
        }
        else if (j.A("property_conflicts").Count > 0)
        {
            var box = Ui.V(6, Ui.T("server.properties does not match this mode:", null, 13.5, Ui.Contrast, FontWeight.SemiBold));
            foreach (var c in j.A("property_conflicts").Items())
                box.Children.Add(Ui.Hint(c.S("key") + " is " + c.S("is", "(empty)") + ", should be " + c.S("want", "(empty)") + " - " + c.S("why")));
            box.Children.Add(Ui.H(10, Ui.Btn("Fix server.properties", () => _ = ApplyProps(), "check", "brand sm"),
                Ui.Hint("Restart the server afterwards for it to take effect.")));
            _modes.Children.Add(Warnish(box, Ui.Orange));
        }
    }

    static Border Warnish(Control child, IBrush edge) => new()
    {
        BorderBrush = edge,
        BorderThickness = new Thickness(3, 0, 0, 0),
        Padding = new Thickness(12, 4, 0, 4),
        Margin = new Thickness(0, 6, 0, 0),
        Child = child,
    };

    // ================================================================ dashboard

    static Control Dot(string state, string text)
    {
        var (col, mark) = state switch
        {
            "good" => (Ui.Brand, "✓ "),
            "bad" => (Ui.Red, "✗ "),
            "warn" => (Ui.Orange, "● "),
            _ => (Ui.Secondary, "● "),
        };
        var t = Ui.T(mark + text, null, 13, col, FontWeight.SemiBold);
        t.TextWrapping = TextWrapping.Wrap;
        t.TextAlignment = TextAlignment.Right;
        return t;
    }

    Control Addr(string? text)
    {
        if (string.IsNullOrEmpty(text)) return Ui.MonoText("—", 13, Ui.Secondary);
        var b = Ui.Btn(text!, () => _ = S.Copy(text!), "copy", "xs ghost", "Click to copy");
        b.FontFamily = Ui.Mono;
        return b;
    }

    void RenderDash(JsonNode j)
    {
        _dash.Children.Clear();
        var running = j.B("running");
        var internet = j.S("mode") == "internet";
        var cs = j.S("connectivity_state", "unknown");
        void K(string k, Control v) => _dash.Children.Add(Ui.KRow(k, v));

        K("Server", Dot(running ? "good" : "off", running ? "ONLINE" : "OFFLINE"));
        K("Mode", Ui.T(j.S("mode_label").ToUpperInvariant(), null, 13, Ui.Contrast, FontWeight.SemiBold));
        K("Minecraft port", Ui.MonoText(j.S("port") + " " + j.S("protocol") + (j.B("different_ports") ? "  (external " + j.S("external_port") + ")" : ""), 13, Ui.Contrast));
        K("This PC", Addr(j.S("local_address")));
        K("Local network", Addr(j.S("lan_address")));

        if (j.S("mode") == "tunnel")
        {
            var t = j.N("tunnel");
            var ng = t.S("provider") == "ngrok";
            var pname = ng ? "ngrok" : "playit.gg";
            K(pname + " agent", t.B("running") ? Dot("good", "Running" + (t.I("uptime") > 0 ? " (" + t.S("uptime") + "s)" : ""))
                : t.B("installed") ? Dot("bad", "Not running") : Dot("bad", "Not installed"));
            if (t.S("agent_path").Length > 0) K("Agent", Ui.MonoText(t.S("agent_path"), 11, Ui.Secondary));
            K("Give friends", Addr(t.S("address")));
            if (t.B("needs_claim") && t.S("claim_url").Length > 0) K("Action needed", Dot("warn", ng ? "Add an authtoken" : "Claim the tunnel"));
            K("External connectivity", Dot(cs == "reachable" ? "good" : cs == "checking" ? "warn" : "bad", j.S("connectivity_label")));
        }

        if (internet)
        {
            var pub = j.N("public_ip");
            K("Public", j.S("public_address").Length > 0 ? Addr(j.S("public_address"))
                : Ui.T(pub.S("ipv4_error", pub.IsFalse("enabled") ? "lookup switched off" : "unknown"), null, 13, Ui.Secondary));
            if (pub.S("ipv4").Length > 0 && !pub.B("ipv4_confirmed"))
                K("", Ui.T("only one source confirmed this address", null, 12.5, Ui.Orange));
            if (pub.S("ipv4_previous").Length > 0) K("Address changed", Ui.MonoText("was " + pub.S("ipv4_previous"), 12.5, Ui.Secondary));
            if (j.S("ipv6_address").Length > 0) K("IPv6", Addr(j.S("ipv6_address")));
            K("Port forward", j.B("mapping_active") ? Dot("good", "Active - " + j.S("mapping_text")) : Dot("bad", "None"));
            var m = j.N("mapping");
            if (m.S("provider").Length > 0)
                K("Made with", Ui.T(m.S("provider") + (m.Has("seconds_left") && m.N("seconds_left") != null
                    ? ", lease " + m.S("seconds_left") + "s left, auto-renewed" : ", permanent"), null, 13, Ui.Text));
            if (j.S("router_external_ip").Length > 0) K("Router WAN", Ui.MonoText(j.S("router_external_ip"), 13, Ui.Text));
            var fw = j.N("firewall");
            if (j.B("windows"))
            {
                var lb = fw.N("likely_blocking");
                K("Windows Firewall", J.Str(lb) == "true" ? Dot("bad", "Not allowing port " + j.S("port"))
                    : J.Str(lb) == "false" ? Dot("good", fw.B("rule_exists") ? "Rule in place" : "Allowed")
                    : Dot("off", fw.S("detail", "not checked")));
            }
            K("External connectivity", Dot(cs == "reachable" ? "good" : cs == "checking" ? "warn" : "bad", j.S("connectivity_label")));
        }

        K("RCON", Dot("off", "Local/LAN only - never forwarded"));
        K("SFTP / SSH", Dot("off", "Local/LAN only - never forwarded"));
        K("Panel", Dot("off", "Port " + j.N("reserved").S("panel") + ", never forwarded"));

        var summary = Ui.V(4, Ui.T(j.S("headline"), null, 14, Ui.Contrast, FontWeight.SemiBold, true));
        if (j.N("connectivity").S("reason").Length > 0) summary.Children.Add(Ui.Hint(j.N("connectivity").S("reason")));
        if (j.S("error").Length > 0) summary.Children.Add(Ui.T(j.S("error"), null, 13, Ui.Red, wrap: true));
        _dash.Children.Add(Ui.Divider1(10));
        _dash.Children.Add(summary);

        if (j.S("mode") == "tunnel")
        {
            var t = j.N("tunnel");
            var ng = t.S("provider") == "ngrok";
            var pname = ng ? "ngrok" : "playit.gg";
            var home = t.S("download", ng ? "https://ngrok.com/download" : "https://playit.gg/download");
            if (!t.B("installed"))
            {
                _dash.Children.Add(Warnish(Ui.V(8,
                    Ui.T("The " + pname + " agent is not installed.", null, 13.5, Ui.Contrast, FontWeight.SemiBold),
                    Ui.Hint("Download it, then point Lodestone at it in Settings below. Lodestone will not download or install it for you."),
                    Ui.Btn(home.Replace("https://", ""), () => MainWindow.OpenUrl(home), "external", "xs")), Ui.Orange));
            }
            else if (t.B("needs_claim") && t.S("claim_url").Length > 0)
            {
                var url = t.S("claim_url");
                _dash.Children.Add(Warnish(Ui.V(8,
                    Ui.T(ng ? "ngrok needs an authtoken." : "Claim the tunnel to finish.", null, 13.5, Ui.Contrast, FontWeight.SemiBold),
                    Ui.Hint(ng
                        ? "Copy yours from the link and run `ngrok config add-authtoken <your token>` in a terminal once. Then press Test " +
                          "connection. Lodestone never asks you for the token and never stores it - it stays in ngrok's own config."
                        : "Open the link, sign in to playit, and approve it. Then press Test connection. Lodestone cannot do this step " +
                          "for you - it needs your playit account."),
                    Ui.Btn(url, () => MainWindow.OpenUrl(url), "external", "xs")), Ui.Orange));
            }
            else if (t.S("address").Length > 0)
            {
                _dash.Children.Add(Ui.Hint("Friends connect to " + t.S("address") + " instead of your IP. This works from behind CGNAT " +
                                           "and needs no port forwarding. Traffic passes through " + pname + " servers, so expect a little extra latency." +
                                           (ng ? " On the free plan this address is reissued every time the agent restarts, so re-send it to friends after a restart." : ""))
                    .Also(h => h.Margin = new Thickness(0, 10, 0, 0)));
            }
            var log = t.Strings("log");
            if (log.Count > 0)
            {
                _dash.Children.Add(Ui.Eyebrow("Agent output").Also(e => e.Margin = new Thickness(0, 12, 0, 6)));
                _dash.Children.Add(Ui.Code(string.Join("\n", log), 11));
            }
        }

        if (internet && cs is "cgnat_suspected" or "ipv6_only")
        {
            _dash.Children.Add(Warnish(Ui.V(6,
                Ui.T("Direct internet hosting unavailable.", null, 13.5, Ui.Contrast, FontWeight.SemiBold),
                Ui.Hint("Your network may be using CGNAT, which prevents incoming connections. Possible solutions:\n" +
                        "• Use IPv6 if your friends' connections support it" +
                        (j.S("ipv6_address").Length > 0 ? " - your address is " + j.S("ipv6_address") : "") + "\n" +
                        "• Ask your ISP for a public IPv4 address - many will provide one on request\n" +
                        "• Configure port forwarding on the outermost router if there are two\n" +
                        "• Use a tunnel, VPN or relay (Tailscale, playit.gg, ngrok, a small VPS)")), Ui.Red));
        }
        if (internet && cs == "port_blocked")
        {
            _dash.Children.Add(Warnish(Ui.V(8,
                Ui.Hint("The router is forwarding the port but Windows is not answering on it."),
                Ui.Btn("Allow through firewall", () => _ = Firewall("add"), "shield", "brand sm")), Ui.Orange));
        }
        if (internet && cs == "server_offline")
            _dash.Children.Add(Ui.Hint("Start the Minecraft server, then test again. Nothing can reach a server that is not running.")
                .Also(h => h.Margin = new Thickness(0, 10, 0, 0)));
    }

    // ================================================================ steps / fix / audit

    void RenderSteps(JsonNode j)
    {
        var steps = j.A("steps").Items().ToList();
        _stepsCard.IsVisible = steps.Count > 0;
        _steps.Children.Clear();
        if (steps.Count == 0) return;
        var names = new Dictionary<string, string>
        {
            ["interfaces"] = "Local addresses", ["properties"] = "server.properties", ["public_ip"] = "Public address",
            ["mapping"] = "Router port forward", ["firewall"] = "Windows Firewall", ["connectivity"] = "Reachable from outside",
            ["done"] = "Result",
        };
        foreach (var s in steps)
        {
            var (st, label) = s.S("state") switch
            {
                "ok" => ("good", "done"),
                "fail" => ("bad", "failed"),
                "warn" => ("warn", "warning"),
                "run" => ("warn", "working…"),
                "skip" => ("off", "skipped"),
                var x => ("off", x),
            };
            _steps.Children.Add(Ui.KRow(names.GetValueOrDefault(s.S("key"), s.S("key")), Dot(st, label)));
            if (s.S("detail").Length > 0) _steps.Children.Add(Ui.Hint(s.S("detail")).Also(h => h.Margin = new Thickness(14, -2, 0, 8)));
        }
        var att = j.A("mapping_attempts").Items().ToList();
        if (att.Count > 1 || (att.Count == 1 && !att[0].B("ok")))
        {
            _steps.Children.Add(Ui.Eyebrow("Port mapping attempts").Also(e => e.Margin = new Thickness(0, 12, 0, 6)));
            foreach (var a in att)
            {
                _steps.Children.Add(Ui.T((a.B("ok") ? "✓ " : "✗ ") + a.S("provider") + " - " + a.S("detail"), null, 13,
                    a.B("ok") ? Ui.Brand : Ui.Text, wrap: true));
                if (a.S("hint").Length > 0) _steps.Children.Add(Ui.Hint(a.S("hint")).Also(h => h.Margin = new Thickness(14, 0, 0, 4)));
            }
        }
    }

    void RenderFix(JsonNode j)
    {
        var f = j.N("autofix");
        _fixCard.IsVisible = f != null;
        _fix.Children.Clear();
        if (f == null) return;
        var fixedList = f.Strings("fixed");
        if (fixedList.Count > 0)
        {
            _fix.Children.Add(Ui.T("Fixed " + fixedList.Count + " thing(s)", null, 14, Ui.Brand, FontWeight.SemiBold));
            foreach (var x in fixedList) _fix.Children.Add(Ui.Hint("✓ " + x));
        }
        else
        {
            _fix.Children.Add(Ui.Hint("Nothing needed fixing on this machine."));
        }
        var manual = f.Strings("manual");
        if (manual.Count > 0)
        {
            _fix.Children.Add(Ui.T("Lodestone cannot do these for you", null, 14, Ui.Orange, FontWeight.SemiBold)
                .Also(t => t.Margin = new Thickness(0, 8, 0, 0)));
            foreach (var x in manual) _fix.Children.Add(Warnish(Ui.Hint(x), Ui.Orange));
        }
        else if (j.B("online"))
        {
            _fix.Children.Add(Ui.T("Everything is working - your server is reachable at " + j.S("public_address") + ".", null, 13.5, Ui.Brand, wrap: true));
        }
    }

    async Task Audit(bool deep)
    {
        JsonNode j;
        try { j = await Api.Get("/api/hosting/security" + (deep ? "?router=1" : "")); }
        catch (Exception) { return; }
        var items = j.A("items").Items().ToList();
        _warnCard.IsVisible = items.Count > 0;
        _warn.Children.Clear();
        foreach (var it in items)
        {
            var col = it.S("level") switch { "bad" => Ui.Red, "warn" => Ui.Orange, _ => Ui.Secondary };
            var v = Ui.V(4, Ui.T(it.S("title"), null, 14, col, FontWeight.SemiBold, true), Ui.Hint(it.S("detail")));
            if (it.S("fix").Length > 0) v.Children.Add(Ui.T(it.S("fix"), null, 13, Ui.Contrast, FontWeight.SemiBold, true));
            _warn.Children.Add(Warnish(v, col));
        }
    }

    // ================================================================ settings

    void RenderSettings(JsonNode j)
    {
        var s = j.N("settings");
        _settings.Children.Clear();
        _port = Ui.Input(j.S("port"), mono: true);
        _extPort = Ui.Input(j.B("different_ports") ? j.S("external_port") : "", "same as the Minecraft port", mono: true);
        var reserved = j.N("reserved");
        var left = Ui.V(0,
            Ui.Field("Minecraft port", _port,
                "Must match server-port in server.properties. Lodestone will not publish port " + reserved.S("panel") + " (the panel)" +
                (reserved.S("rcon").Length > 0 ? ", " + reserved.S("rcon") + " (RCON)" : "") + " or 22 (SFTP/SSH)."),
            Ui.Field("External port", _extPort,
                "Leave empty unless your ISP blocks 25565 or you host two servers. Friends then connect to the external port."));

        _provider = Ui.Select(new[] { ("playit", "playit.gg"), ("ngrok", "ngrok") }, s.S("tunnel_provider") == "ngrok" ? "ngrok" : "playit", v =>
        {
            _playitRow.IsVisible = v != "ngrok";
            _ngrokRow.IsVisible = v == "ngrok";
        });
        left.Children.Add(Ui.Field("Tunnel provider", _provider,
            "Which agent Tunnel mode runs. Both dial out, so neither needs anything opened on your router. playit.gg is approved " +
            "once in a browser; ngrok is authenticated once with ngrok config add-authtoken. Lodestone asks for neither credential " +
            "and stores neither."));
        _agent = Ui.Input(s.S("tunnel_agent"), "found automatically", mono: true);
        _ngrok = Ui.Input(s.S("ngrok_agent"), "found automatically", mono: true);
        _playitRow = Ui.Field("playit.gg agent", Browse(_agent), "Only needed if Lodestone cannot find it. Get it from playit.gg/download.");
        _ngrokRow = Ui.Field("ngrok agent", Browse(_ngrok),
            "Only needed if Lodestone cannot find it. Get it from ngrok.com/download, then run ngrok config add-authtoken <your token> once.");
        _playitRow.IsVisible = s.S("tunnel_provider") != "ngrok";
        _ngrokRow.IsVisible = s.S("tunnel_provider") == "ngrok";
        left.Children.Add(_playitRow);
        left.Children.Add(_ngrokRow);

        _upnp = Ui.Switch("UPnP IGD", s.B("upnp"));
        _natpmp = Ui.Switch("NAT-PMP", s.B("natpmp"));
        _pcp = Ui.Switch("PCP", s.B("pcp"));
        _fw = Ui.Switch("Manage the Windows Firewall rule", s.B("firewall"));
        _pubIp = Ui.Switch("Look up my public address", s.B("public_ip_lookup"));
        _ext = Ui.Switch("Test reachability from outside", s.B("external_test"));
        _auto = Ui.Switch("Re-enable internet hosting when Lodestone starts", s.B("auto_enable"));
        var right = Ui.V(0,
            Ui.SwitchHint(_upnp, "The one nearly every home router speaks."),
            Ui.SwitchHint(_natpmp, "RFC 6886. Tried when UPnP is off or refuses."),
            Ui.SwitchHint(_pcp, "RFC 6887. Newer, and the one some CGNAT boxes answer."),
            Ui.SwitchHint(_fw, "Creates one inbound allow rule for this port only. Never disables the firewall. Needs an administrator prompt."),
            Ui.SwitchHint(_pubIp, "Asks api.ipify.org and icanhazip.com what address the internet sees. Nothing but the request itself is sent."),
            Ui.SwitchHint(_ext, "Asks mcstatus.io and mcsrvstat.us to connect to your address and port. This is the only way to prove the " +
                                "server is publicly reachable. With it off, the status can never rise above \"unknown\"."),
            Ui.SwitchHint(_auto, "Off by default. The port is opened only while Lodestone is running either way."));
        _settings.Children.Add(Ui.Cols(32, left, right));
        _settings.Children.Add(Ui.Divider1(8));
        _settings.Children.Add(Ui.Hint("What gets published: only the Minecraft port, only TCP, and only while internet mode is on. RCON, SFTP, " +
                                       "SSH and this panel are never forwarded - the port picker refuses them outright. Turning internet mode off " +
                                       "removes the mapping Lodestone created and leaves every other rule on your router alone."));
        if (j.B("windows"))
        {
            var fw = j.N("firewall");
            var row = new WrapPanel { Margin = new Thickness(0, 12, 0, 0) };
            foreach (var c in new Control[]
                     {
                         Ui.Btn("Check firewall", () => _ = Firewall("check"), "shield", "sm"),
                         Ui.Btn("Allow port " + j.S("port"), () => _ = Firewall("add"), "check", "sm"),
                         Ui.Btn("Remove Lodestone's rule", () => _ = Firewall("remove"), "trash", "soft-red sm"),
                         Ui.Pill(fw.S("rule", "no rule"), "", false),
                         fw.B("admin") ? Ui.Pill("running as admin", "on", false) : new Control(),
                     })
            {
                c.Margin = new Thickness(0, 0, 8, 6);
                row.Children.Add(c);
            }
            _settings.Children.Add(row);
        }
    }

    Grid Browse(TextBox box)
    {
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        g.Children.Add(box);
        var b = Ui.Btn("Browse", async () =>
        {
            var r = await S.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions { Title = "Select the agent" });
            var p = r.FirstOrDefault()?.TryGetLocalPath();
            if (p != null)
            {
                box.Text = p;
                Toasts.Good("Picked. Press Save.");
            }
        }, "folder", "sm");
        b.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(b, 1);
        g.Children.Add(b);
        return g;
    }

    async Task SaveSettings()
    {
        int.TryParse(_port.Text, out var port);
        int.TryParse(_extPort.Text, out var ext);
        var body = new JsonObject
        {
            ["port"] = port, ["external_port"] = ext,
            ["upnp"] = _upnp.IsChecked == true, ["natpmp"] = _natpmp.IsChecked == true, ["pcp"] = _pcp.IsChecked == true,
            ["firewall"] = _fw.IsChecked == true, ["public_ip_lookup"] = _pubIp.IsChecked == true,
            ["external_test"] = _ext.IsChecked == true, ["auto_enable"] = _auto.IsChecked == true,
            ["tunnel_provider"] = Ui.Val(_provider) ?? "playit",
            ["tunnel_agent"] = (_agent.Text ?? "").Trim(), ["ngrok_agent"] = (_ngrok.Text ?? "").Trim(),
        };
        if (await Ops.Act("/api/hosting/settings", body, "Saved.") != null) await Load();
    }

    // ================================================================ actions

    async Task SetMode(string mode)
    {
        if (mode == "internet")
        {
            var text = "Lodestone will ask your router to forward TCP port " + (_net?.S("port", "25565") ?? "25565") +
                       " to this PC, then check from outside whether it worked.\n\n" +
                       "Anyone who learns your address can attempt to connect. Minecraft servers on public addresses are scanned " +
                       "constantly and get join attempts from strangers within hours. Turn on the whitelist if you only want your friends in.\n\n" +
                       "Only the Minecraft port is opened. RCON, SFTP and this panel stay on your network. Turning internet mode off " +
                       "again removes the forwarding rule.";
            if (!await Dialogs.Confirm("Publish this server to the internet?", text, "Publish", "brand")) return;
            if (await Ops.Act("/api/hosting/mode", new { mode = "internet" }, "Setting up internet hosting…") == null) return;
        }
        else
        {
            int.TryParse(_port?.Text, out var port);
            if (port == 0) port = _net.I("port");
            if (await Ops.Act("/api/hosting/mode", new { mode, port }, "Switching…") == null) return;
        }
        await Load();
        StartPoll();
    }

    async void Disable()
    {
        if (!await Dialogs.Confirm("Turn off internet hosting?",
                "The port mapping Lodestone created will be removed from your router. Other rules are left alone. The server keeps " +
                "running and stays reachable on your local network.", "Turn off"))
            return;
        await Ops.Act("/api/hosting/disable", new { remove_firewall = false }, "Internet hosting off.");
        await Load();
    }

    async void AutoFix()
    {
        var text = "Lodestone will, in order:\n" +
                   "• correct server-ip and server-port if they do not match this mode\n" +
                   "• start the Minecraft server if it is not running\n" +
                   "• ask your router for the port forward if there is not one\n" +
                   "• add the Windows Firewall rule if nothing allows the port (asks for admin)\n" +
                   "• test again from outside\n\n" +
                   "It cannot fix everything. A router upstream of yours, CGNAT, or an ISP that blocks inbound connections are all " +
                   "outside this machine's reach - anything it cannot do, it will tell you about instead of pretending.\n\n" +
                   "Starting a server can take a minute or two.";
        if (!await Dialogs.Confirm("Run auto fix?", text, "Run it", "brand")) return;
        if (await Ops.Act("/api/hosting/autofix", null, "Auto fix running…") == null) return;
        await Load();
        StartPoll();
    }

    async Task Test()
    {
        if (await Ops.Act("/api/hosting/test", new { deep_firewall = true }, "Checking…") == null) return;
        await Load();
        StartPoll();
    }

    async Task Probe()
    {
        JsonNode j;
        try { j = await Api.Get("/api/hosting/probe"); }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
            return;
        }
        var body = Ui.V(10, Ui.KRow("Gateway", j.S("gateway", "not found")));
        foreach (var p in j.A("providers").Items())
            body.Children.Add(Ui.Row(Ui.TitleSub(p.S("provider"), p.S("protocol") + " - " + p.S("detail")),
                Ui.Pill(p.B("available") ? "supported" : "no", p.B("available") ? "on" : "bad")));
        var rows = j.A("router_table").Items().ToList();
        body.Children.Add(Ui.Eyebrow("Everything your router currently forwards (" + rows.Count + ")").Also(e => e.Margin = new Thickness(0, 8, 0, 0)));
        body.Children.Add(Ui.Hint("Read only - nothing here was changed."));
        body.Children.Add(rows.Count > 0
            ? Ui.Code(string.Join("\n", rows.Select(r => r.S("protocol") + " " + r.S("external_port") + " → " + r.S("internal_client") + ":" +
                                                          r.S("internal_port") + "  " + r.S("description"))))
            : Ui.Hint("Nothing, or your router will not list them."));
        await Dialogs.Show("Router support", body, 640, ("Close", "", ""));
    }

    async Task Firewall(string action)
    {
        var j = await Ops.Act("/api/hosting/firewall", new { action }, action == "check" ? null : "Done.");
        if (j == null) return;
        if (action == "check")
        {
            var r = j.N("result");
            var names = string.Join(", ", r.A("allowing_rules").Items().Select(x => x.S("name")));
            var st = J.Str(r.N("state"));
            var body = Ui.V(8, Ui.T(r.S("detail"), null, 14, Ui.Text, wrap: true),
                Ui.KRow("Firewall on for this profile", st == "true" ? "yes" : st == "false" ? "no" : "unknown"),
                Ui.KRow("Current profile", r.S("current_profile", "unknown")),
                Ui.KRow("Lodestone rule", (r.B("rule_exists") ? "present" : "not created") + " (" + r.S("rule") + ")"));
            if (names.Length > 0) body.Children.Add(Ui.Hint("Rules already allowing this port: " + names));
            await Dialogs.Info("Windows Firewall", body);
        }
        await Load();
    }

    async Task ApplyProps()
    {
        var j = await Ops.Act("/api/hosting/properties", null, "server.properties updated - restart the server to apply.");
        if (j != null && !j.B("changed")) Toasts.Good("It was already correct.");
        await Load();
    }
}
