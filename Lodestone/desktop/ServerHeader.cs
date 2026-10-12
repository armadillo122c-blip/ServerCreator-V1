using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop;

/// <summary>
/// The top of every server tab, laid out like a Modrinth project page: the
/// server icon, its name in heavy type, a row of tags, and the actions on the
/// right - Start in brand green, Stop in red.
/// </summary>
public sealed class ServerHeader
{
    readonly MainWindow _s;
    public Grid View { get; }
    readonly Border _icon;
    readonly TextBlock _name;
    readonly Border _state;
    readonly WrapPanel _tags;
    readonly StackPanel _actions;
    string _sig = "";
    string _tagSig = "";
    Border? _mhPill;
    string _iconFor = "";

    public ServerHeader(MainWindow s)
    {
        _s = s;
        _icon = Ui.Rounded(Icons.Make("box", 34, Ui.Secondary), 76, 18, Ui.Raised);
        _icon.BorderBrush = Ui.Divider;
        _icon.BorderThickness = new Thickness(1);
        _name = Ui.H1("—");
        _name.TextTrimming = TextTrimming.CharacterEllipsis;
        _state = Ui.Pill("offline");
        _tags = new WrapPanel { Orientation = Orientation.Horizontal };
        _actions = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, VerticalAlignment = VerticalAlignment.Center };

        var info = Ui.V(9, Ui.H(12, _name, _state), _tags);
        info.VerticalAlignment = VerticalAlignment.Center;
        info.Margin = new Thickness(18, 0, 18, 0);

        View = new Grid
        {
            ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto"),
            Margin = new Thickness(28, 24, 28, 6),
        };
        View.Children.Add(_icon);
        Grid.SetColumn(info, 1);
        View.Children.Add(info);
        Grid.SetColumn(_actions, 2);
        View.Children.Add(_actions);
    }

    public void Update(JsonNode s)
    {
        var srv = s.N("server");
        var id = srv.S("id");
        _name.Text = srv.S("name", "server");
        var running = s.B("running");
        var st = s.S("state", "offline");
        // A local server is running or stopped; its folder being found is not "connected".
        var local = srv.S("kind") == "local";
        var label = running ? "Running" : local && st is "online" or "" ? "Stopped" : st switch
        {
            "online" => "Connected",
            "error" => "Error",
            "connecting" => "Connecting",
            "" => "Offline",
            _ => char.ToUpperInvariant(st[0]) + st[1..],
        };
        Ui.SetPill(_state, label, running || (!local && st == "online") ? "on" : st == "error" ? "bad" : st == "connecting" ? "warn" : "");

        if (id + "|" + srv.S("icon") != _iconFor)
        {
            _iconFor = id + "|" + srv.S("icon");
            RefreshIcon();
        }

        // Tags change slowly; rebuilding them only when they differ keeps
        // tooltips and hover states from flickering every 1.5 s.
        var tr = s.N("transport");
        var kind = srv.S("kind");
        var where = tr.S("root").Length > 0 ? tr.S("root") : tr.S("host");
        var tagSig = string.Join("|", kind, where, srv.S("software"), srv.S("mc"), s.B("minehut"), s.I("player_count"), s.S("uptime_h"), running, s.B("can_command"));
        if (tagSig != _tagSig)
        {
            _tagSig = tagSig;
            _tags.Children.Clear();
            void Add(Control c)
            {
                c.Margin = new Thickness(0, 0, 6, 4);
                _tags.Children.Add(c);
            }
            Add(Ui.Tag(kind == "local" ? "Local" : "Remote · SFTP", kind == "local" ? "drive" : "globe"));
            var sw = srv.S("software");
            if (sw.Length > 0) Add(Ui.Tag(Pages.ServersPage.SoftwareName(sw) + " " + srv.S("mc"), "box"));
            if (s.B("minehut")) Add(Ui.Tag("Minehut", "server"));
            if (where.Length > 0)
            {
                var t = Ui.Tag(where.Length > 46 ? "…" + where[^45..] : where, "folder");
                ToolTip.SetTip(t, where);
                Add(t);
            }
            Add(Ui.Tag(Ui.Plural(s.I("player_count"), "player") + " online", "users",
                s.I("player_count") > 0 ? Ui.Brand : null));
            if (s.L("uptime") > 0) Add(Ui.Tag("up " + s.S("uptime_h"), "clock"));
            if (!s.B("can_command")) Add(Ui.Tag("console read-only", "info"));
        }

        // a Minehut server shows its status even without a login (Minehut's public info - minehut_id); the Start /
        // Stop buttons need the login, so they only come with the Minehut section turned on
        var mhId = s.S("minehut_id");
        var sig = string.Join("|", s.B("can_start"), s.B("eula"), running, tr.S("state"), s.B("minehut"), mhId);
        if (sig == _sig) return;
        _sig = sig;
        _actions.Children.Clear();
        _mhPill = null;
        if (s.B("minehut") || mhId.Length > 0)
        {
            _mhPill = Ui.Pill("minehut", "");
            ToolTip.SetTip(_mhPill, "Minehut");
            _actions.Children.Add(_mhPill);
            if (s.B("minehut"))
            {
                _actions.Children.Add(Ui.Btn("Start", () => MhDo("start"), "play", "brand lg"));
                _actions.Children.Add(Ui.Btn("Restart", () => MhDo("restart"), "restart", "lg"));
                _actions.Children.Add(Ui.Btn("Stop", () => MhDo("stop"), "stop", "danger lg"));
                _actions.Children.Add(Ui.IconBtn("moon", "Force hibernate", () => MhDo("hibernate"), "", 42));
            }
            _ = RefreshMinehut();
        }
        if (s.B("can_start") && !s.B("minehut"))
        {
            if (!s.B("eula"))
                _actions.Children.Add(Ui.Btn("Accept EULA", AcceptEula, "shield", "soft-orange lg"));
            if (running)
            {
                _actions.Children.Add(Ui.Btn("Restart", () => _ = Ops.Act("/api/restart", null, "Restarting."), "restart", "lg"));
                _actions.Children.Add(Ui.Btn("Stop", () => _ = Ops.Act("/api/stop", null, "Stopping."), "stop", "danger lg"));
                _actions.Children.Add(Ui.IconBtn("zap", "Kill - stops the process immediately", Kill, "soft-red", 42));
            }
            else
            {
                _actions.Children.Add(Ui.Btn("Start", () => _ = Ops.Act("/api/start", null, "Starting."), "play", "brand lg"));
            }
        }
        else if (!s.B("can_start"))
        {
            if (tr.S("state") == "online")
            {
                _actions.Children.Add(Ui.Btn("Reconnect", () => _ = Ops.Act("/api/connect", null, "Reconnected."), "refresh", "lg"));
                _actions.Children.Add(Ui.Btn("Disconnect", () => _ = Ops.Act("/api/disconnect", null, "Disconnected."), "unplug", "soft-red lg"));
            }
            else
            {
                _actions.Children.Add(Ui.Btn("Connect", () => _ = Ops.Act("/api/connect", null, "Connected."), "plug", "brand lg"));
            }
        }
    }

    public async void RefreshIcon()
    {
        var srv = _s.State.N("server");
        var icon = srv.S("icon");
        if (icon.Length > 0)
        {
            // the instance's item or block, as in the library and the sidebar
            _icon.Child = Images.Icon(icon, 74, 16);
            return;
        }
        var bmp = await Images.ServerIcon();
        var sw = srv.S("software");
        _icon.Child = bmp != null
            ? Ui.PixelImage(bmp, 76)
            : sw.Length > 0
                ? Images.Art(sw, 74, 16)
                : Icons.Make("box", 34, Ui.Secondary);
    }

    async void MhDo(string what)
    {
        if (what == "hibernate" &&
            !await Dialogs.Confirm("Hibernate the server?",
                "Minehut stops it immediately and puts it to sleep. Players are disconnected.", "Hibernate"))
            return;
        await Ops.Act("/api/minehut/action", new { what }, null);
        await Task.Delay(1500);
        await RefreshMinehut();
    }

    public async Task RefreshMinehut()
    {
        if ((!_s.State.B("minehut") && _s.State.S("minehut_id").Length == 0) || _mhPill == null) return;
        JsonNode j;
        try { j = await Api.Post("/api/minehut/status", new { }); }
        catch (ApiException e)
        {
            Ui.SetPill(_mhPill, "minehut: error", "bad");
            ToolTip.SetTip(_mhPill, e.Message);
            return;
        }
        catch (Exception) { return; }
        var state = j.B("online") ? "online" : j.B("starting") ? "starting" : j.B("hibernating") ? "asleep" : "offline";
        Ui.SetPill(_mhPill, "minehut " + state + " · " + j.S("players", "0") + "/" + j.S("max_players", "?"),
            j.B("online") ? "on" : j.B("hibernating") ? "" : "warn");
        ToolTip.SetTip(_mhPill, string.Join(" · ", new[] { j.S("name"), j.S("plan"), j.S("version"), j.S("address") }
            .Where(x => x.Length > 0)));
    }

    async void Kill()
    {
        if (await Dialogs.Confirm("Kill the server?",
                "This stops the process immediately - anything unsaved in the world is lost. Use Stop unless it has hung.",
                "Kill it"))
            await Ops.Act("/api/kill", null, "Killed.");
    }

    async void AcceptEula()
    {
        if (await Dialogs.Confirm("Accept the Minecraft EULA?",
                "This writes eula=true into eula.txt. Only do this if you accept Mojang's EULA at aka.ms/MinecraftEULA.",
                "Accept", "brand"))
            await Ops.Act("/api/eula", null, "EULA accepted.");
    }
}
