using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// A terminal for the workspace: your commands, in this computer's shell (PowerShell on Windows), in the
/// workspace folder, with the JDK Lodestone installed on PATH for builds. Each command is a job in the engine
/// (/api/dev/terminal), its output streamed here; Stop ends exactly that process tree. cd changes the folder
/// commands run in; clear empties the screen; Up and Down walk the history.
/// </summary>
public sealed partial class CodePage
{
    SelectableTextBlock _termOut = null!;
    ScrollViewer _termScroll = null!;
    TextBox _termIn = null!;
    TextBlock _termPrompt = null!;
    Button _termStop = null!;
    string? _termJob;
    string _termCwd = ".";
    readonly List<string> _termHist = new();
    int _termPos = -1;
    int _termLines;

    Control BuildTerminal()
    {
        _termOut = new SelectableTextBlock
        {
            FontFamily = Ui.Mono,
            FontSize = 13,
            TextWrapping = TextWrapping.Wrap,
            Foreground = LogView.Plain,
            Inlines = new InlineCollection(),
        };
        _termScroll = new ScrollViewer { Content = _termOut, Padding = new Thickness(16, 12) };
        _termPrompt = Ui.T("›", null, 13.5, Ui.Brand, FontWeight.Bold).Also(t =>
        {
            t.FontFamily = Ui.Mono;
            t.VerticalAlignment = VerticalAlignment.Center;
            t.Margin = new Thickness(12, 0, 4, 0);
        });
        _termIn = Ui.Input("", "Type a command - Enter runs it", mono: true);
        _termIn.InnerLeftContent = _termPrompt;
        _termIn.AddHandler(InputElement.KeyDownEvent, TermKey, Avalonia.Interactivity.RoutingStrategies.Tunnel);
        _termStop = Ui.Btn("Stop", () => _ = TermStop(), "stop", "soft-red sm");
        _termStop.IsVisible = false;
        var row = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto,Auto"), Margin = new Thickness(0, 10, 0, 0) };
        row.Children.Add(_termIn);
        var clear = Ui.Btn("Clear", TermClear, "trash", "ghost sm");
        foreach (var (b, col) in new[] { (_termStop, 1), (clear, 2) })
        {
            b.Margin = new Thickness(8, 0, 0, 0);
            Grid.SetColumn(b, col);
            row.Children.Add(b);
        }
        var screen = new Border
        {
            Background = Ui.Terminal,
            CornerRadius = new CornerRadius(12),
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1),
            ClipToBounds = true,
            Child = _termScroll,
        };
        var g = new Grid { RowDefinitions = new RowDefinitions("*,Auto") };
        g.Children.Add(screen);
        Grid.SetRow(row, 1);
        g.Children.Add(row);
        TermLine("This workspace's terminal. Commands run in its folder, with the JDK Lodestone installed (if any) on PATH.", LogView.Time);
        return g;
    }

    void TermLine(string text, IBrush? brush = null, bool bold = false)
    {
        var (clean, runs) = LogView.StripCodes(text);
        if (_termLines > 0) _termOut.Inlines!.Add(new LineBreak());
        if (runs.Count == 0)
            _termOut.Inlines!.Add(new Run(clean) { Foreground = brush ?? LogView.Plain, FontWeight = bold ? FontWeight.Bold : FontWeight.Normal });
        else
        {
            var at = 0;
            foreach (var r in runs)
            {
                if (r.Start > at) _termOut.Inlines!.Add(new Run(clean[at..r.Start]) { Foreground = brush ?? LogView.Plain });
                _termOut.Inlines!.Add(new Run(clean[r.Start..r.End]) { Foreground = r.Brush, FontWeight = r.Bold ? FontWeight.Bold : FontWeight.Normal });
                at = r.End;
            }
            if (at < clean.Length) _termOut.Inlines!.Add(new Run(clean[at..]) { Foreground = brush ?? LogView.Plain });
        }
        _termLines++;
        if (_termOut.Inlines!.Count > 6000)
        {
            while (_termOut.Inlines.Count > 4000) _termOut.Inlines.RemoveAt(0);
        }
        Dispatcher.UIThread.Post(() => _termScroll.ScrollToEnd(), DispatcherPriority.Background);
    }

    void TermClear()
    {
        _termOut.Inlines!.Clear();
        _termLines = 0;
    }

    void TermKey(object? sender, KeyEventArgs e)
    {
        if (e.Key == Avalonia.Input.Key.Enter)
        {
            e.Handled = true;
            _ = TermRun();
        }
        else if (e.Key == Avalonia.Input.Key.Up && _termHist.Count > 0)
        {
            e.Handled = true;
            _termPos = Math.Min(_termPos + 1, _termHist.Count - 1);
            _termIn.Text = _termHist[_termPos];
            _termIn.CaretIndex = _termIn.Text.Length;
        }
        else if (e.Key == Avalonia.Input.Key.Down)
        {
            e.Handled = true;
            _termPos = Math.Max(_termPos - 1, -1);
            _termIn.Text = _termPos < 0 ? "" : _termHist[_termPos];
            _termIn.CaretIndex = _termIn.Text?.Length ?? 0;
        }
        else if (e.Key == Avalonia.Input.Key.C && e.KeyModifiers.HasFlag(KeyModifiers.Control) && _termJob != null
                 && string.IsNullOrEmpty(_termIn.SelectedText))
        {
            e.Handled = true;
            _ = TermStop();
        }
    }

    async Task TermRun()
    {
        var cmd = (_termIn.Text ?? "").Trim();
        if (cmd.Length == 0 || _termJob != null) return;
        if (_ws.Length == 0)
        {
            Toasts.Bad("Pick a workspace first.");
            return;
        }
        _termHist.Insert(0, cmd);
        _termPos = -1;
        _termIn.Text = "";
        TermLine((_termCwd == "." ? "" : _termCwd + " ") + "› " + cmd, Ui.Brand, true);
        if (cmd is "clear" or "cls")
        {
            TermClear();
            return;
        }
        if (cmd == "cd" || cmd.StartsWith("cd ", StringComparison.Ordinal))
        {
            var to = cmd.Length > 2 ? cmd[3..].Trim().Trim('"') : ".";
            var next = to is "" or "~" ? "." : Path.IsPathRooted(to) ? to : Path.Combine(_termCwd == "." ? "" : _termCwd, to);
            next = next.Replace('\\', '/');
            // the engine checks it on the next command; an empty run here checks it now
            try
            {
                var j = await Api.Post("/api/dev/terminal", new JsonObject { ["ws"] = _ws, ["command"] = "echo ok", ["cwd"] = next });
                _termCwd = next.Length == 0 ? "." : next;
                _termPrompt.Text = "›";
                ToolTip.SetTip(_termIn, j.Path("job.cwd")?.ToString());
            }
            catch (Exception ex)
            {
                TermLine(ex.Message, LogView.ErrText);
            }
            return;
        }
        JsonNode started;
        try { started = await Api.Post("/api/dev/terminal", new JsonObject { ["ws"] = _ws, ["command"] = cmd, ["cwd"] = _termCwd }); }
        catch (Exception ex)
        {
            TermLine(ex.Message, LogView.ErrText);
            return;
        }
        _termJob = started.Path("job.id") is { } id ? J.Str(id) : null;
        _termStop.IsVisible = true;
        long since = 0;
        while (_termJob != null)
        {
            JsonNode job;
            try { job = (await Api.Get("/api/jobs?id=" + Api.Q(_termJob) + "&since=" + since)).N("job")!; }
            catch (Exception ex)
            {
                TermLine(ex.Message, LogView.ErrText);
                break;
            }
            since = job.L("next");
            foreach (var line in job.Strings("log")) TermLine(line);
            if (job.S("state") != "running")
            {
                var exit = job.Path("result.exit")?.ToString() ?? "?";
                var killed = job.Path("result.killed")?.ToString() ?? "";
                TermLine(killed.Length > 0 ? "■ stopped: " + killed : "exit " + exit,
                    exit == "0" && killed.Length == 0 ? LogView.Time : LogView.ErrLvl);
                break;
            }
            await Task.Delay(250);
        }
        _termJob = null;
        _termStop.IsVisible = false;
        _termIn.Focus();
    }

    async Task TermStop()
    {
        if (_termJob == null) return;
        await Ops.Act("/api/dev/build/cancel", new JsonObject { ["ws"] = _ws, ["job"] = _termJob }, null);
    }
}
