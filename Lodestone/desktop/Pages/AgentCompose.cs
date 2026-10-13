using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// The bar above the message box: how this message is worked on (Ask - approvals as set; Agent - carried
/// through without stopping for approvals; Research - read-only and the web, ending in a report with sources)
/// and which model answers it. Plus pictures as attachments, for models that can see them.
/// </summary>
public sealed partial class AgentPage
{
    string _chatMode = "normal";
    ComboBox? _modelPick;
    readonly Dictionary<string, Button> _chatModeBtns = new();

    static readonly (string key, string label, string icon, string tip)[] ChatModes =
    {
        ("normal", "Ask", "message", "Works on your request; changes and commands wait for your approval as set in Model & resources"),
        ("agent", "Agent", "zap", "Carries the whole task through on its own - builds, tests and fixes without stopping for approvals. " +
                                  "Your access settings still apply, and the Minecraft EULA is always yours to accept."),
        ("research", "Research", "search", "Only reads - the project, and the web and GitHub when they are on - and answers with a report and its sources. Changes nothing."),
    };

    Control ComposerBar()
    {
        var seg = Ui.H(2);
        foreach (var (key, label, icon, tip) in ChatModes)
        {
            var b = Ui.Btn(label, () => SetChatMode(key), icon, "tab", tip);
            b.Classes.Set("active", key == _chatMode);
            _chatModeBtns[key] = b;
            seg.Children.Add(b);
        }
        var modes = new Border { Background = Ui.ButtonBg, CornerRadius = new CornerRadius(9), Padding = new Thickness(3), Child = seg };
        _modelPick = new ComboBox { MinWidth = 220, MaxWidth = 320 };
        ToolTip.SetTip(_modelPick, "The model for this message - the default is the one chosen in Model & resources");
        PaintModelPick();
        return Ui.Split(modes, Ui.H(8, Icons.Make("cpu", 15, Ui.Secondary), _modelPick));
    }

    void SetChatMode(string key)
    {
        _chatMode = key;
        foreach (var (k, b) in _chatModeBtns) b.Classes.Set("active", k == key);
        _input.Watermark = key switch
        {
            "agent" => "Describe the whole task - it will plan, change, build and test until it works. Enter sends.",
            "research" => "Ask a question - it reads the project and the web, and answers with sources. Enter sends.",
            _ => "Ask about the project, or describe what to build or fix. Enter sends, Shift+Enter adds a line.",
        };
    }

    void PaintModelPick()
    {
        if (_modelPick == null) return;
        var chosen = Ui.Val(_modelPick) ?? "";
        var def = _state.Path("settings.model") is { } m ? J.Str(m) : "";
        var opts = new List<(string, string)> { ("", def.Length > 0 ? "Default: " + def : "Default model") };
        foreach (var x in _ollama?.A("models").Items() ?? Enumerable.Empty<JsonNode>())
        {
            var name = x.S("name");
            if (name.Length == 0 || name == def) continue;
            var caps = x.A("capabilities").Items().Select(c => J.Str(c)).ToList();
            var tags = (caps.Contains("vision") ? " · sees images" : "") + (x.B("tools") ? "" : " · no tools");
            opts.Add((name, name + tags));
        }
        _modelPick.ItemsSource = null;
        var items = opts.Select(o => new ComboBoxItem { Content = o.Item2, Tag = o.Item1 }).ToList();
        _modelPick.ItemsSource = items;
        _modelPick.SelectedItem = items.FirstOrDefault(i => (string?)i.Tag == chosen) ?? items[0];
    }

    void ModeNote(JsonNode ev)
    {
        var mode = ev.S("mode");
        if (mode == "normal") return;
        var (label, brush) = mode == "agent" ? ("Agent mode - working through it without stopping for approvals", Ui.Brand)
                                             : ("Research mode - reading only, answering with sources", Ui.Blue);
        _msgs.Children.Add(new Border
        {
            Background = mode == "agent" ? Ui.BrandHighlight : Ui.BlueHighlight,
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(10, 5),
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(4, 0),
            Child = Ui.H(8, Icons.Make(mode == "agent" ? "zap" : "search", 14, brush),
                Ui.T(label, null, 12.5, brush, FontWeight.SemiBold), Ui.T(ev.S("model"), null, 12, Ui.Secondary)),
        });
        ScrollEnd();
    }

    static bool IsImage(string name) =>
        name.EndsWith(".png", StringComparison.OrdinalIgnoreCase) || name.EndsWith(".jpg", StringComparison.OrdinalIgnoreCase) ||
        name.EndsWith(".jpeg", StringComparison.OrdinalIgnoreCase) || name.EndsWith(".webp", StringComparison.OrdinalIgnoreCase);

    async Task AttachImage(string name, Stream s)
    {
        using var ms = new MemoryStream();
        await s.CopyToAsync(ms);
        if (ms.Length > 8_000_000)
        {
            Toasts.Bad("That picture is over 8 MB - make it smaller first.");
            return;
        }
        _atts.Add(new JsonObject { ["name"] = name, ["image"] = Convert.ToBase64String(ms.ToArray()) });
        RenderAtts();
        var model = _modelPick == null ? "" : Ui.Val(_modelPick) ?? "";
        var info = _ollama?.A("models").Items().FirstOrDefault(m => m.S("name") == (model.Length > 0 ? model
            : _state.Path("settings.model") is { } d ? J.Str(d) : ""));
        if (info != null && !info.A("capabilities").Items().Any(c => J.Str(c) == "vision"))
            Toasts.Show("This model cannot see pictures - pick one marked 'sees images' above.", "warn");
    }
}
