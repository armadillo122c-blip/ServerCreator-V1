using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>Restarts, backups, broadcasts, timed commands and macros for this server.</summary>
public sealed class AutomationPage : Page
{
    public AutomationPage(MainWindow s) : base(s) { }

    public override string Key => "auto";
    public override string Group => "Automate";
    public override string Title => "Automation";
    public override string Icon => "clock";
    public override bool Advanced => true;

    JsonObject _f = new();
    readonly List<(string path, Func<JsonNode?> read)> _fields = new();
    StackPanel _autoBody = null!, _tasks = null!, _macros = null!;
    readonly List<(TextBox time, TextBox cmd)> _taskRows = new();
    readonly List<(TextBox name, TextBox cmds)> _macroRows = new();

    protected override Control Build()
    {
        _autoBody = Ui.V(0);
        _tasks = Ui.V(8);
        _macros = Ui.V(8);
        var auto = Ui.Card("Automation", "Runs while Lodestone is running", Ui.Btn("Save", () => _ = Save(), "save", "brand"), _autoBody);
        var tasks = Ui.Card("Timed commands", "Times are on this machine's clock, checked every 10 seconds",
            Ui.Btn("Add", AddTask, "plus", "sm"), _tasks);
        var macros = Ui.Card("Macros", "One click on the console runs every line in order",
            Ui.Btn("Add", AddMacro, "plus", "sm"), _macros);
        return Ui.Scroll(Ui.V(16, auto, tasks, macros), new Thickness(28, 16, 28, 28));
    }

    public override async Task OnShow()
    {
        var j = await Ops.Get("/api/features");
        if (j is not JsonObject o) return;
        _f = o;
        S.Features = o;
        Render();
    }

    Control Tog(string path, string label, string? hint = null)
    {
        var sw = Ui.Switch(label, J.Truthy(_f.Path(path)));
        _fields.Add((path, () => sw.IsChecked == true));
        return Ui.SwitchHint(sw, hint);
    }

    Control Num(string path, string label, double width = 130)
    {
        var tb = Ui.Input(J.Str(_f.Path(path)), width: width);
        _fields.Add((path, () =>
        {
            var v = (tb.Text ?? "").Trim();
            return long.TryParse(v, out var n) && v.All(char.IsDigit) ? JsonValue.Create(n) : JsonValue.Create(v);
        }));
        return Ui.Field(label, tb).Also(c => c.Margin = new Thickness(0, 0, 12, 14));
    }

    Control Txt(string path, string label)
    {
        var tb = Ui.Input(J.Str(_f.Path(path)));
        _fields.Add((path, () => tb.Text ?? ""));
        return Ui.Field(label, tb);
    }

    static Control Indent(params Control[] kids)
    {
        var s = Ui.H(0, kids);
        s.Margin = new Thickness(53, -4, 0, 4);
        s.VerticalAlignment = VerticalAlignment.Top;
        return s;
    }

    void Render()
    {
        _fields.Clear();
        _autoBody.Children.Clear();
        var left = Ui.V(2,
            Tog("auto_restart", "Restart after a crash", "Relaunches the server if it exits on its own."),
            Tog("daily_restart.enabled", "Daily restart"),
            Indent(Num("daily_restart.time", "At (HH:MM)")),
            Tog("watchdog.enabled", "Watchdog", "Restart if the console goes silent - catches a hung server."),
            Indent(Num("watchdog.minutes", "Silent for (min)")),
            Tog("auto_backup.enabled", "Scheduled backups"),
            Indent(Num("auto_backup.hours", "Every (hours)"), Num("auto_backup.keep", "Keep last")));
        var right = Ui.V(2,
            Tog("auto_save.enabled", "Periodic save-all"),
            Indent(Num("auto_save.minutes", "Every (min)")),
            Tog("broadcast.enabled", "Repeating broadcast"),
            Indent(Num("broadcast.minutes", "Every (min)")),
            Txt("broadcast.message", "Broadcast message").Also(c => c.Margin = new Thickness(53, 0, 0, 14)),
            Tog("clearlag.enabled", "Clear dropped items"),
            Indent(Num("clearlag.minutes", "Every (min)")),
            Tog("clearlag.warn", "Warn 10 seconds first"),
            Tog("welcome.enabled", "Welcome message"),
            Txt("welcome.message", "Says (use {player})").Also(c => c.Margin = new Thickness(53, 0, 0, 0)));
        _autoBody.Children.Add(Ui.Cols(32, left, right));
        RenderTasks();
        RenderMacros();
    }

    void RenderTasks()
    {
        _tasks.Children.Clear();
        _taskRows.Clear();
        foreach (var t in _f.A("tasks").Items()) AddTaskRow(t.S("time"), t.S("command"));
        if (_taskRows.Count == 0) _tasks.Children.Add(Ui.Empty("Nothing scheduled.", "clock"));
    }

    void AddTaskRow(string time, string cmd)
    {
        if (_taskRows.Count == 0) _tasks.Children.Clear();
        var tb = Ui.Input(time, "HH:MM", mono: true, width: 100);
        var cb = Ui.Input(cmd, "say something / backup / restart", mono: true);
        (TextBox, TextBox) pair = (tb, cb);
        _taskRows.Add(pair);
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto") };
        g.Children.Add(tb);
        cb.Margin = new Thickness(8, 0, 8, 0);
        Grid.SetColumn(cb, 1);
        g.Children.Add(cb);
        Border row = null!;
        var rm = Ui.IconBtn("trash", "Remove", () =>
        {
            _taskRows.Remove(pair);
            _tasks.Children.Remove(row);
        }, "soft-red", 36);
        Grid.SetColumn(rm, 2);
        g.Children.Add(rm);
        row = new Border { Child = g };
        row.Classes.Add("row");
        _tasks.Children.Add(row);
    }

    void AddTask() => AddTaskRow("04:00", "say scheduled task");

    void RenderMacros()
    {
        _macros.Children.Clear();
        _macroRows.Clear();
        foreach (var m in _f.A("macros").Items()) AddMacroRow(m.S("name"), m.S("commands"));
        if (_macroRows.Count == 0) _macros.Children.Add(Ui.Empty("No macros yet.", "zap"));
    }

    void AddMacroRow(string name, string cmds)
    {
        if (_macroRows.Count == 0) _macros.Children.Clear();
        var nb = Ui.Input(name, "Name");
        var cb = Ui.Area(cmds, 3, "one command per line", true);
        (TextBox, TextBox) pair = (nb, cb);
        _macroRows.Add(pair);
        Border row = null!;
        var rm = Ui.IconBtn("trash", "Remove", () =>
        {
            _macroRows.Remove(pair);
            _macros.Children.Remove(row);
        }, "soft-red", 36);
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        g.Children.Add(Ui.V(8, nb, cb));
        rm.Margin = new Thickness(8, 0, 0, 0);
        rm.VerticalAlignment = VerticalAlignment.Top;
        Grid.SetColumn(rm, 1);
        g.Children.Add(rm);
        row = new Border { Child = g };
        row.Classes.Add("row");
        _macros.Children.Add(row);
    }

    void AddMacro() => AddMacroRow("New macro", "");

    async Task Save()
    {
        foreach (var (path, read) in _fields) _f.SetPath(path, read());
        var tasks = new JsonArray();
        foreach (var (t, c) in _taskRows)
            if ((t.Text ?? "").Length > 0 && (c.Text ?? "").Length > 0)
                tasks.Add(new JsonObject { ["time"] = t.Text, ["command"] = c.Text });
        _f["tasks"] = tasks;
        var macros = new JsonArray();
        foreach (var (n, c) in _macroRows)
            if ((n.Text ?? "").Length > 0)
                macros.Add(new JsonObject { ["name"] = n.Text, ["commands"] = c.Text ?? "" });
        _f["macros"] = macros;
        if (await Ops.Act("/api/features", _f, "Automation saved.") != null)
        {
            S.Features = _f;
            S.Console.RenderChips();
        }
    }
}
