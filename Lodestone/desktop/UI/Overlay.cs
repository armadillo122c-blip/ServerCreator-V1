using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Animation;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using Lodestone.Desktop.Core;

namespace Lodestone.Desktop.UI;

/// <summary>Bottom-right notifications, like Modrinth's.</summary>
public static class Toasts
{
    static StackPanel? _host;

    public static void Init(StackPanel host) => _host = host;

    static readonly Dictionary<string, DateTime> Recent = new();

    public static void Show(string? msg, string kind = "")
    {
        if (_host == null || string.IsNullOrWhiteSpace(msg)) return;
        Dispatcher.UIThread.Post(() =>
        {
            // Several pages failing at once say the same thing; once is enough.
            var key = kind + "|" + msg;
            var now = DateTime.UtcNow;
            if (Recent.TryGetValue(key, out var at) && now - at < TimeSpan.FromSeconds(8)) return;
            Recent[key] = now;
            if (Recent.Count > 50)
                foreach (var k in Recent.Where(kv => now - kv.Value > TimeSpan.FromSeconds(8)).Select(kv => kv.Key).ToList())
                    Recent.Remove(k);
            Add(msg!, kind);
        });
    }

    public static void Good(string? msg) => Show(msg, "good");
    public static void Bad(string? msg) => Show(msg, "bad");

    static void Add(string msg, string kind)
    {
        var (icon, col) = kind switch
        {
            "good" => ("circle-check", Ui.Brand),
            "bad" => ("circle-x", Ui.Red),
            _ => ("info", Ui.Blue),
        };
        Sounds.Play(kind switch { "good" => "success", "bad" => "error", _ => "info" });
        var text = Ui.T(msg, null, 13.5, Ui.Contrast, wrap: true);
        text.MaxWidth = 340;
        var card = new Border
        {
            Background = Ui.Raised,
            BorderBrush = Ui.Ring,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(14),
            Padding = new Thickness(14, 12, 16, 12),
            BoxShadow = BoxShadows.Parse("0 8 24 0 #66000000"),
            Child = Ui.H(11, Icons.Make(icon, 20, col), text),
            Opacity = 0,
            RenderTransform = Avalonia.Media.Transformation.TransformOperations.Parse("translateX(24px)"),
            Transitions = new Transitions
            {
                new DoubleTransition { Property = Visual.OpacityProperty, Duration = TimeSpan.FromMilliseconds(180) },
                new TransformOperationsTransition { Property = Visual.RenderTransformProperty, Duration = TimeSpan.FromMilliseconds(180) },
            },
        };
        card.PointerPressed += (_, _) => _host!.Children.Remove(card);
        _host!.Children.Add(card);
        while (_host.Children.Count > 5) _host.Children.RemoveAt(0);
        Dispatcher.UIThread.Post(() =>
        {
            card.Opacity = 1;
            card.RenderTransform = Avalonia.Media.Transformation.TransformOperations.Parse("translateX(0px)");
        }, DispatcherPriority.Background);
        DispatcherTimer.RunOnce(() =>
        {
            card.Opacity = 0;
            DispatcherTimer.RunOnce(() => _host.Children.Remove(card), TimeSpan.FromMilliseconds(200));
        }, TimeSpan.FromMilliseconds(4200));
    }
}

/// <summary>
/// Modal dialogs drawn inside the window: a dimmed backdrop and one centred
/// card. One at a time - opening another answers the first with null.
/// </summary>
public static class Dialogs
{
    static Panel? _host;
    static TaskCompletionSource<string?>? _open;

    public static bool IsOpen => _open != null;

    public static void Init(Panel host)
    {
        _host = host;
        host.IsVisible = false;
    }

    public static Task<string?> Show(string title, Control body, params (string label, string id, string cls)[] buttons) =>
        Show(title, body, 560, buttons);

    public static Task<string?> Show(string title, Control body, double width,
                                     params (string label, string id, string cls)[] buttons)
    {
        Close(null);
        var tcs = new TaskCompletionSource<string?>();
        _open = tcs;

        var foot = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 8,
            HorizontalAlignment = HorizontalAlignment.Right,
        };
        foreach (var (label, id, cls) in buttons)
        {
            var b = Ui.Btn(label, () => Close(id), null, cls);
            foot.Children.Add(b);
        }
        var head = Ui.Split(Ui.H2(title).Also(t => t.TextWrapping = TextWrapping.Wrap),
            Ui.IconBtn("x", "Close", () => Close(null), "ghost round", 32));
        var scroller = new ScrollViewer
        {
            Content = body,
            MaxHeight = 560,
            HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled,
        };
        var col = Ui.V(18, head, scroller);
        if (foot.Children.Count > 0) col.Children.Add(foot);
        var card = new Border
        {
            Background = Ui.Raised,
            BorderBrush = Ui.Ring,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(18),
            Padding = new Thickness(24, 20, 24, 20),
            MaxWidth = width,
            Margin = new Thickness(24),
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
            BoxShadow = BoxShadows.Parse("0 16 48 0 #99000000"),
            Child = col,
        };
        // clicks on the card must not reach the backdrop
        card.PointerPressed += (_, e) => e.Handled = true;

        _host!.Children.Clear();
        var backdrop = new Border { Background = Ui.Hex("#A6000000") };
        backdrop.PointerPressed += (_, _) => Close(null);
        _host.Children.Add(backdrop);
        _host.Children.Add(card);
        _host.IsVisible = true;
        Anim.FadeIn(backdrop, 160);
        Anim.Pop(card);
        Sounds.Play("pop");
        return tcs.Task;
    }

    public static void Close(string? result)
    {
        var t = _open;
        _open = null;
        if (_host != null)
        {
            _host.IsVisible = false;
            _host.Children.Clear();
        }
        t?.TrySetResult(result);
    }

    public static void HandleKey(KeyEventArgs e)
    {
        if (_open == null) return;
        if (e.Key == Key.Escape)
        {
            Close(null);
            e.Handled = true;
        }
    }

    public static async Task<bool> Confirm(string title, string text, string ok = "Confirm", string cls = "danger")
    {
        var body = Ui.T(text, null, 14, Ui.Text, wrap: true);
        body.LineHeight = 21;
        return await Show(title, body, ("Cancel", "", ""), (ok, "ok", cls)) == "ok";
    }

    public static async Task<string?> Prompt(string title, string label, string value = "", string ok = "OK")
    {
        var box = Ui.Input(value);
        var body = Ui.Field(label, box);
        box.OnEnter(() => Close("ok"));
        Dispatcher.UIThread.Post(() =>
        {
            box.Focus();
            box.SelectAll();
        }, DispatcherPriority.Background);
        return await Show(title, body, ("Cancel", "", ""), (ok, "ok", "brand")) == "ok" ? box.Text ?? "" : null;
    }

    public static Task Info(string title, Control body) => Show(title, body, ("Close", "", ""));
}

/// <summary>Calls with the old page's act() behaviour: toast the result, swallow the error.</summary>
public static class Ops
{
    /// <param name="ok">Toast text on success; "" means the reply's msg or "Done.", null means silent.</param>
    public static async Task<JsonNode?> Act(string path, object? body = null, string? ok = "")
    {
        try
        {
            var j = await Api.Post(path, body ?? new { });
            if (ok != null)
            {
                var m = j.S("msg");
                Toasts.Good(m.Length > 0 ? m : ok.Length > 0 ? ok : "Done.");
            }
            return j;
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
            return null;
        }
    }

    public static async Task<JsonNode?> Get(string path, bool toast = true)
    {
        try
        {
            return await Api.Get(path);
        }
        catch (Exception e)
        {
            if (toast) Toasts.Bad(e.Message);
            return null;
        }
    }
}
