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

/// <summary>
/// What the server and this computer are doing, measured - never estimated.
/// CPU and memory come from the operating system (the server's whole process
/// tree), TPS and MSPT only from the server's own commands, and anything that
/// cannot be measured says so and why. Development Performance Mode lives here
/// too, since it trades the panel's background work for the coding model.
/// </summary>
public sealed class PerfPage : Page
{
    public PerfPage(MainWindow s) : base(s) { }

    public override string Key => "perf";
    public override string Title => "Performance";
    public override string Icon => "gauge";

    StackPanel _root = null!;
    DispatcherTimer? _timer;
    DispatcherTimer? _tpsTimer;
    JsonNode? _last;
    ToggleButton _autoTps = null!, _perfMode = null!;
    bool _painting;

    // tiles
    TextBlock _tState = null!, _tStateSub = null!, _tCpu = null!, _tCpuSub = null!, _tMem = null!, _tMemSub = null!,
        _tTps = null!, _tTpsSub = null!, _tMspt = null!, _tMsptSub = null!, _tPl = null!, _tPlSub = null!;
    StackPanel _host = null!, _gpu = null!, _ollama = null!, _tpsInfo = null!;

    protected override Control Build()
    {
        var tiles = new UniformGrid { Columns = 3, Margin = new Thickness(0, 0, 0, 2) };
        tiles.Children.Add(Tile("Server", "server", out _tState, out _tStateSub));
        tiles.Children.Add(Tile("CPU (server process)", "cpu", out _tCpu, out _tCpuSub));
        tiles.Children.Add(Tile("Memory (server process)", "drive", out _tMem, out _tMemSub));
        tiles.Children.Add(Tile("TPS", "activity", out _tTps, out _tTpsSub));
        tiles.Children.Add(Tile("MSPT", "timer", out _tMspt, out _tMsptSub));
        tiles.Children.Add(Tile("Players", "users", out _tPl, out _tPlSub));

        _autoTps = Ui.Switch("Measure every 30 seconds while this page is open", false, on =>
        {
            _tpsTimer?.Stop();
            _tpsTimer = null;
            if (on)
            {
                _tpsTimer = new DispatcherTimer(TimeSpan.FromSeconds(30), DispatcherPriority.Background, (_, _) => _ = Measure(true));
                _tpsTimer.Start();
                _ = Measure(true);
            }
        });
        _tpsInfo = Ui.V(6);
        var tpsCard = Ui.Card("Tick rate", "Asked from the server itself. The command and its answer appear in the console.",
            Ui.Btn("Measure now", () => _ = Measure(false), "activity", "brand sm"),
            Ui.V(8, _tpsInfo, Ui.SwitchHint(_autoTps, null)));

        _host = Ui.V(0);
        _gpu = Ui.V(6);
        _ollama = Ui.V(6);
        _perfMode = Ui.Switch("Development Performance Mode", false, on =>
        {
            if (_painting) return;
            _ = SetPerfMode(on);
        });
        var modeCard = Ui.Card(Ui.V(10,
            Ui.SwitchHint(_perfMode, "For coding with a local model on this computer. Turn it off again when you are done."),
            Markdown.Render(
                "- one assistant task at a time, so tasks never fight over the model\n" +
                "- the model stays loaded between requests (30 minutes), so it is not loaded twice\n" +
                "- background project indexing pauses\n" +
                "- AI chat moderation waits (nothing is lost - it catches up afterwards)\n" +
                "- the automatic update check waits\n" +
                "- animations in this window stop and this page refreshes less often", null, null),
            Ui.Hint("It changes only Lodestone and its own work. It never touches other programs.")), 18);

        var left = Ui.V(16, tpsCard, Ui.Card("This computer", null, null, _host), Ui.Card("GPU", null, null, _gpu));
        var right = Ui.V(16, modeCard, Ui.Card("Ollama", "Models loaded right now, from Ollama itself.", null, _ollama));
        _root = Ui.V(16, tiles, Ui.Cols(16, left, right));
        return Ui.Scroll(_root, new Thickness(28, 16, 28, 28));
    }

    static Border Tile(string label, string icon, out TextBlock value, out TextBlock sub)
    {
        value = Ui.T("—", null, 22, Ui.Contrast, FontWeight.Bold);
        sub = Ui.T("", null, 12, Ui.Secondary, wrap: true);
        var badge = new Border
        {
            Width = 36, Height = 36, CornerRadius = new CornerRadius(10), Background = Ui.ButtonBg,
            Child = Icons.Make(icon, 18).Also(i =>
            {
                i.HorizontalAlignment = HorizontalAlignment.Center;
                i.VerticalAlignment = VerticalAlignment.Center;
            }),
        };
        var card = Ui.Card(Ui.Lead(badge, Ui.V(1, Ui.T(label, null, 12.5, Ui.Secondary), value, sub)), 14);
        card.Margin = new Thickness(0, 0, 12, 12);
        return card;
    }

    public override async Task OnShow()
    {
        await Refresh();
        _timer?.Stop();
        _timer = new DispatcherTimer(TimeSpan.FromSeconds(Anim.Enabled ? 2 : 5), DispatcherPriority.Background, (_, _) => _ = Refresh());
        _timer.Start();
    }

    public override void OnHide()
    {
        _timer?.Stop();
        _timer = null;
        _tpsTimer?.Stop();
        _tpsTimer = null;
        _autoTps.IsChecked = false;
    }

    public override void OnServerSwitched()
    {
        _autoTps.IsChecked = false;
        if (Showing) _ = Refresh();
    }

    async Task Refresh()
    {
        var sid = S.State.Path("server.id") is { } id ? J.Str(id) : "";
        var j = await Ops.Get("/api/perf" + (sid.Length > 0 ? "?sid=" + Api.Q(sid) : ""), false);
        if (j == null) return;
        _last = j;
        Paint(j);
    }

    static string Gb(long bytes) => (bytes / 1073741824.0).ToString(bytes >= 10L * 1073741824 ? "0" : "0.0") + " GB";
    static string Mb(long bytes) => bytes >= 1073741824 ? Gb(bytes) : (bytes / 1048576.0).ToString("0") + " MB";

    void Paint(JsonNode j)
    {
        _painting = true;
        try
        {
            var s = j.N("server") ?? new JsonObject();
            var running = s.B("running");
            _tState.Text = running ? "running" : "stopped";
            _tState.Foreground = running ? Ui.Brand : Ui.Secondary;
            _tStateSub.Text = running ? "up " + Dur(s.L("uptime")) : s.S("kind") == "local" ? "start it to see live numbers" : "remote server";

            var p = s.N("process");
            if (p != null)
            {
                var cpu = p.N("cpu_percent");
                _tCpu.Text = cpu == null ? "…" : J.Str(cpu) + "%";
                _tCpuSub.Text = cpu == null ? "measuring - needs two readings" :
                    "of the whole computer · " + J.Str(p.N("cpu_core_percent")) + "% of one core · " + Ui.Plural(p.L("pids"), "process", "processes");
                _tMem.Text = Mb(p.L("rss"));
                var alloc = s.L("allocated_mb");
                _tMemSub.Text = alloc > 0 ? "of " + (alloc >= 1024 ? (alloc / 1024.0).ToString("0.#") + " GB" : alloc + " MB") +
                                            " allowed (-Xmx) · the JVM itself also needs some outside the heap"
                    : "no -Xmx set for this server";
            }
            else
            {
                _tCpu.Text = "—";
                _tCpuSub.Text = s.S("process_why");
                _tMem.Text = "—";
                var alloc = s.L("allocated_mb");
                _tMemSub.Text = s.S("process_why") + (alloc > 0 ? " Allowed: " + alloc + " MB." : "");
            }

            var tps = s.N("tps");
            if (tps != null)
            {
                var t = tps.N("tps");
                var one = t?.N("1m") ?? t?.N("mean");
                _tTps.Text = one == null ? "—" : J.Str(one);
                var v = one == null ? 20 : double.Parse(J.Str(one), System.Globalization.CultureInfo.InvariantCulture);
                _tTps.Foreground = v >= 19.5 ? Ui.Brand : v >= 16 ? Ui.Orange : Ui.Red;
                _tTpsSub.Text = (t?.N("5m") != null ? "5m " + J.Str(t.N("5m")) + " · 15m " + J.Str(t.N("15m")) + " · " : "") +
                                "measured " + Dur(tps.L("age")) + " ago" + (tps.S("derived").Length > 0 ? " · " + tps.S("derived") : "");
                var ms = tps.N("mspt");
                var m1 = ms?.N("1m") ?? ms?.N("mean");
                _tMspt.Text = m1 == null ? "—" : J.Str(m1.N("avg")) + " ms";
                _tMspt.Foreground = m1 == null ? Ui.Secondary : m1.D("avg") < 40 ? Ui.Brand : m1.D("avg") < 50 ? Ui.Orange : Ui.Red;
                _tMsptSub.Text = m1 == null ? "this server's command does not report it"
                    : (m1.Has("max") ? "max " + J.Str(m1.N("max")) + " ms · " : "") + "50 ms is the most a tick may take";
            }
            else
            {
                _tTps.Text = "—";
                _tTps.Foreground = Ui.Contrast;
                _tTpsSub.Text = s.S("tps_why").Length > 0 ? s.S("tps_why") : running ? "press Measure now" : "measured only while running";
                _tMspt.Text = "—";
                _tMspt.Foreground = Ui.Contrast;
                var asks = s.Strings("tps_commands");
                _tMsptSub.Text = asks.Count == 0 ? "" :
                    asks.Count == 1 && asks[0] == "tps" ? "Spigot's tps command does not report it" :
                    running ? "press Measure now" : "measured only while running";
            }
            _tPl.Text = running ? s.S("player_count", "0") : "—";
            _tPlSub.Text = running ? s.S("player_source") : "";

            _tpsInfo.Children.Clear();
            var cmds = s.Strings("tps_commands");
            _tpsInfo.Children.Add(Ui.T(cmds.Count > 0 ? "Asks with: " + string.Join(", ", cmds.Select(c => "/" + c)) : s.S("tps_why"),
                null, 12.5, Ui.Secondary, wrap: true));

            // host
            var h = j.N("host") ?? new JsonObject();
            _host.Children.Clear();
            var total = h.L("memory_total");
            var avail = h.L("memory_available");
            if (total > 0)
            {
                var used = total - avail;
                _host.Children.Add(Ui.KRow("Memory", Gb(used) + " used of " + Gb(total)));
                _host.Children.Add(Bar((double)used / total));
                _host.Children.Add(Ui.KRow("Available", Gb(avail)));
            }
            else
                _host.Children.Add(Ui.KRow("Memory", "not measurable on this system"));
            _host.Children.Add(Ui.KRow("CPU", h.N("cpu_percent") is { } c ? J.Str(c) + "% busy" : "measuring…"));
            _host.Children.Add(Ui.KRow("Cores", h.S("cores", "?")));

            _gpu.Children.Clear();
            var g = j.N("gpu") ?? new JsonObject();
            if (!g.B("available"))
                _gpu.Children.Add(Ui.T(g.S("why", "No GPU information."), null, 12.5, Ui.Secondary, wrap: true));
            foreach (var gi in g.A("items").Items())
            {
                _gpu.Children.Add(Ui.T(gi.S("name"), "h3"));
                if (gi.L("memory_total") > 0)
                {
                    _gpu.Children.Add(Ui.KRow("VRAM", Mb(gi.L("memory_used")) + " used of " + Mb(gi.L("memory_total"))));
                    _gpu.Children.Add(Bar((double)gi.L("memory_used") / gi.L("memory_total")));
                }
                if (gi.N("utilization") is { } u) _gpu.Children.Add(Ui.KRow("Busy", J.Str(u) + "%"));
            }

            var ol = j.N("ollama") ?? new JsonObject();
            _ollama.Children.Clear();
            if (!ol.B("running"))
                _ollama.Children.Add(Ui.T("Not running" + (ol.S("error").Length > 0 ? ": " + ol.S("error") : "."), null, 12.5, Ui.Secondary, wrap: true));
            else
            {
                _ollama.Children.Add(Ui.KRow("Version", ol.S("version")));
                var loaded = ol.A("loaded").Items().ToList();
                if (loaded.Count == 0) _ollama.Children.Add(Ui.Hint("No model loaded right now."));
                foreach (var m in loaded)
                {
                    var vram = m.L("size_vram");
                    var size = m.L("size");
                    var where = vram <= 0 ? "CPU" : vram >= size ? "GPU" : (100.0 * vram / Math.Max(1, size)).ToString("0") + "% GPU";
                    var name = m.S("name");
                    _ollama.Children.Add(Ui.Row(Ui.TitleSub(name, Gb(size) + " · " + where + (m.L("context_length") > 0 ? " · context " + m.S("context_length") : "")),
                        Ui.Btn("Unload", () => _ = Ops.Act("/api/agent/model", new JsonObject { ["action"] = "unload", ["model"] = name }), null, "sm")));
                }
                var busy = j.Path("assistant.active");
                if (busy != null && J.Str(busy) != "0") _ollama.Children.Add(Ui.Pill(J.Str(busy) + " assistant task(s) running", "info"));
            }
            _perfMode.IsChecked = j.B("perf_mode");
        }
        finally
        {
            _painting = false;
        }
    }

    static Control Bar(double frac)
    {
        frac = Math.Clamp(frac, 0, 1);
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions($"{frac.ToString(System.Globalization.CultureInfo.InvariantCulture)}*,{(1 - frac).ToString(System.Globalization.CultureInfo.InvariantCulture)}*"), Height = 6, Margin = new Thickness(0, 2, 0, 6) };
        var fill = new Border { Background = frac > 0.9 ? Ui.Red : frac > 0.75 ? Ui.Orange : Ui.Brand, CornerRadius = new CornerRadius(3) };
        var bg = new Border { Background = Ui.ButtonBg, CornerRadius = new CornerRadius(3) };
        Grid.SetColumnSpan(bg, 2);
        g.Children.Add(bg);
        g.Children.Add(fill);
        return g;
    }

    static string Dur(long s) => s < 60 ? s + "s" : s < 3600 ? s / 60 + "m" : s / 3600 + "h " + s % 3600 / 60 + "m";

    async Task Measure(bool quiet)
    {
        var sid = S.State.Path("server.id") is { } id ? J.Str(id) : "";
        try
        {
            await Api.Post("/api/perf/measure", new JsonObject { ["sid"] = sid });
            await Refresh();
        }
        catch (Exception e)
        {
            if (!quiet) Toasts.Bad(e.Message);
            else _autoTps.IsChecked = false;
        }
    }

    async Task SetPerfMode(bool on)
    {
        var j = await Ops.Act("/api/agent/settings", new JsonObject { ["perf_mode"] = on },
            on ? "Development Performance Mode is on." : "Development Performance Mode is off.");
        if (j != null) S.ApplyPerfMode(on);
        await Refresh();
    }
}
