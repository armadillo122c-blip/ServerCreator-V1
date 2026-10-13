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

/// <summary>The AI assistant: a chat on the left, its provider and permissions on the right.</summary>
public sealed class AssistantPage : Page
{
    public AssistantPage(MainWindow s) : base(s) { }

    public override string Key => "ai";
    public override string Group => "Automate";
    public override string Title => "Admin assistant";
    public override string Icon => "sparkles";
    public override bool Advanced => true;

    JsonNode _ai = new JsonObject();
    readonly List<JsonObject> _atts = new();

    TextBlock _model = null!, _usage = null!;
    StackPanel _msgs = null!;
    ScrollViewer _msgScroll = null!;
    WrapPanel _attRow = null!;
    TextBox _input = null!;
    Button _go = null!;
    StackPanel _settings = null!;

    // settings controls
    ComboBox _prov = null!;
    AutoCompleteBox _modelIn = null!;
    TextBlock _modelHint = null!;
    TextBox _key = null!, _base = null!, _rules = null!;
    Slider _temp = null!;
    ToggleButton _cmds = null!, _files = null!, _mod = null!, _modAuto = null!;
    bool _saving;
    Control? _intro;

    protected override Control Build()
    {
        _model = Ui.T("—", null, 12.5, Ui.Secondary);
        _usage = Ui.T("", null, 12.5, Ui.Secondary);
        _msgs = Ui.V(12);
        _msgs.Margin = new Thickness(18, 16, 18, 16);
        _intro = Intro();
        _msgs.Children.Add(_intro);
        _msgScroll = new ScrollViewer { Content = _msgs, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
        var msgBox = new Border
        {
            Background = Ui.Terminal,
            CornerRadius = new CornerRadius(12),
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1),
            Margin = new Thickness(14, 0, 14, 0),
            Child = _msgScroll,
        };

        _attRow = new WrapPanel();
        _input = Ui.Input("", "Set the difficulty to hard and tell everyone why").OnEnter(Send);
        _go = Ui.Btn("Send", Send, "send", "brand");
        var compose = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto,Auto") };
        compose.Children.Add(_input);
        var attach = Ui.IconBtn("paperclip", "Attach a file from this PC", Attach, "", 38);
        attach.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(attach, 1);
        compose.Children.Add(attach);
        _go.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(_go, 2);
        compose.Children.Add(_go);
        var bottom = Ui.V(8, _attRow, compose);
        bottom.Margin = new Thickness(14, 12, 14, 14);

        var body = new Grid { RowDefinitions = new RowDefinitions("*,Auto") };
        body.Children.Add(msgBox);
        Grid.SetRow(bottom, 1);
        body.Children.Add(bottom);
        var chat = Ui.FillCard(
            Ui.Split(Ui.H(10, Ui.H2("Assistant"), _model), Ui.H(10, _usage, Ui.Btn("Clear chat", () => _ = Clear(), "trash", "ghost sm"))),
            body);

        _settings = Ui.V(0);
        var side = Ui.Card(Ui.V(14,
            Ui.Split(Ui.H2("Settings"), Ui.H(6, Ui.Btn("Test", () => _ = Test(), null, "sm"),
                Ui.Btn("Save", () => _ = SaveSettings(), "save", "brand sm"))),
            _settings), 18);
        var sideScroll = new ScrollViewer { Content = side, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };

        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("*,16,360"), Margin = new Thickness(28, 16, 28, 24) };
        g.Children.Add(chat);
        Grid.SetColumn(sideScroll, 2);
        g.Children.Add(sideScroll);
        return g;
    }

    static Control Intro() => Ui.Empty(
        "Ask it to change a setting, read the logs, write a Skript file, or diagnose a crash. It works the server through the " +
        "same console and files this panel uses - so it drives remote servers over RCON and SFTP too.", "sparkles");

    public override Task OnShow() => Load();

    async Task Load()
    {
        var j = await Ops.Get("/api/ai");
        if (j == null) return;
        _ai = j;
        _model.Text = j.B("has_key") || j.B("local")
            ? j.S("provider", "openai") + " · " + j.S("model", "default model")
            : "no API key yet";
        var u = j.N("usage");
        _usage.Text = u != null && (u.I("in") > 0 || u.I("out") > 0) ? "tokens " + u.S("in") + " in / " + u.S("out") + " out" : "";
        RenderSettings();
    }

    void RenderSettings()
    {
        _settings.Children.Clear();
        var local = _ai.B("local");
        _prov = Ui.Select(new[]
        {
            ("--cloud", "Cloud"), ("openai", "OpenAI / compatible"), ("anthropic", "Anthropic"), ("gemini", "Google Gemini"),
            ("--local", "Local - no key needed"), ("ollama", "Ollama"), ("lmstudio", "LM Studio"),
            ("llamacpp", "llama.cpp server"), ("custom", "Other OpenAI-compatible"),
        }, _ai.S("provider", "openai"));
        _prov.SelectionChanged += async (_, _) =>
        {
            if (_saving) return;
            await SaveSettings(); // the engine has to know the provider before it can probe it
        };
        _settings.Children.Add(Ui.Field("Provider", _prov,
            local ? "Runs on your own machine. Nothing leaves it, and no key is needed." : null));

        _modelIn = new AutoCompleteBox
        {
            Text = _ai.S("model"),
            Watermark = local ? "press Detect" : "leave blank for the default",
            FilterMode = AutoCompleteFilterMode.Contains,
            MinimumPrefixLength = 0,
        };
        _modelHint = Ui.Hint("");
        var modelRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        modelRow.Children.Add(_modelIn);
        var detect = Ui.Btn("Detect", () => _ = Detect(), null, "sm");
        detect.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(detect, 1);
        modelRow.Children.Add(detect);
        _settings.Children.Add(Ui.V(0, Ui.Label("Model"), modelRow, _modelHint.Also(h => h.Margin = new Thickness(0, 6, 0, 14))));

        _key = Ui.Input("", _ai.B("has_key") ? "saved - type to replace" : "paste your key", password: true);
        if (_ai.B("needs_key"))
            _settings.Children.Add(Ui.Field("API key", _key, "Your own key, stored in ~/.lodestone.json on this machine."));
        _base = Ui.Input(_ai.S("base_url"), _ai.S("base_default"), mono: true);
        _settings.Children.Add(Ui.Field("Base URL", _base));

        var t = _ai.Has("temperature") ? _ai.D("temperature") : 0.4;
        var tv = Ui.Pill(t.ToString("0.0"), "", false);
        _temp = new Slider { Minimum = 0, Maximum = 1.4, Value = t, TickFrequency = 0.1, IsSnapToTickEnabled = true };
        _temp.PropertyChanged += (_, e) =>
        {
            if (e.Property == RangeBase.ValueProperty) Ui.SetPill(tv, _temp.Value.ToString("0.0"), "", false);
        };
        var tempRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        tempRow.Children.Add(_temp);
        tv.Margin = new Thickness(10, 0, 0, 0);
        Grid.SetColumn(tv, 1);
        tempRow.Children.Add(tv);
        _settings.Children.Add(Ui.Field("Temperature", tempRow, "Lower is steadier. The assistant has to emit strict JSON, so keep it low."));

        _settings.Children.Add(Ui.Divider1(6));
        _cmds = Ui.Switch("May run commands", !_ai.IsFalse("allow_commands"));
        _files = Ui.Switch("May edit files", _ai.B("allow_files"));
        _settings.Children.Add(Ui.SwitchHint(_cmds, null));
        _settings.Children.Add(Ui.SwitchHint(_files, "Off means it can still read and explain, but not change anything."));
        _settings.Children.Add(Ui.Divider1(6));
        _mod = Ui.Switch("Watch chat for rule breaks", _ai.B("automod"));
        _modAuto = Ui.Switch("Act on it automatically", _ai.B("automod_auto"));
        _settings.Children.Add(Ui.SwitchHint(_mod, null));
        _settings.Children.Add(Ui.SwitchHint(_modAuto, "With this off, flags are logged and sent to Discord for a human to judge."));
        _rules = Ui.Area(_ai.S("rules"), 4);
        _settings.Children.Add(Ui.Field("Rules", _rules));
    }

    JsonObject Collect()
    {
        var body = new JsonObject
        {
            ["provider"] = Ui.Val(_prov) ?? "openai",
            ["model"] = _modelIn.Text ?? "",
            ["base_url"] = _base.Text ?? "",
            ["allow_commands"] = _cmds.IsChecked == true,
            ["allow_files"] = _files.IsChecked == true,
            ["automod"] = _mod.IsChecked == true,
            ["automod_auto"] = _modAuto.IsChecked == true,
            ["rules"] = _rules.Text ?? "",
            ["temperature"] = Math.Round(_temp.Value, 1),
        };
        if (!string.IsNullOrEmpty(_key.Text)) body["api_key"] = _key.Text;
        return body;
    }

    async Task SaveSettings(bool quiet = false)
    {
        _saving = true;
        try
        {
            await Ops.Act("/api/ai/save", Collect(), quiet ? null : "Assistant settings saved.");
            await Load();
        }
        finally
        {
            _saving = false;
        }
    }

    async Task Detect()
    {
        _modelHint.Text = "asking the server…";
        await SaveSettings(true);
        try
        {
            var j = await Api.Post("/api/ai/models", new { });
            if (!j.B("ok"))
            {
                _modelHint.Text = j.S("error", "could not list models");
                return;
            }
            var models = j.Strings("models");
            _modelIn.ItemsSource = models;
            _modelHint.Text = models.Count > 0
                ? models.Count + " model(s) at " + j.S("base") + " - click the box to pick one"
                : "reachable, but no models installed at " + j.S("base");
            if (models.Count > 0 && string.IsNullOrEmpty(_modelIn.Text)) _modelIn.Text = models[0];
        }
        catch (Exception e)
        {
            _modelHint.Text = e.Message;
        }
    }

    async Task Test()
    {
        Push("step", "testing the connection…");
        try
        {
            var j = await Api.Post("/api/ai/test", new { });
            Push(j.B("ok") ? "step" : "err", j.S("msg"));
        }
        catch (Exception e)
        {
            Push("err", e.Message);
        }
    }

    Control Push(string kind, string text, string? who = null)
    {
        if (_intro != null)
        {
            _msgs.Children.Remove(_intro);
            _intro = null;
        }
        Control c;
        if (kind == "step")
        {
            var t = Ui.MonoText(text, 12.5, Ui.Secondary);
            t.TextWrapping = TextWrapping.Wrap;
            c = new Border
            {
                BorderBrush = Ui.Ring,
                BorderThickness = new Thickness(2, 0, 0, 0),
                Padding = new Thickness(12, 2),
                Child = t,
            };
        }
        else
        {
            var me = kind == "me";
            var err = kind == "err";
            var body = Ui.Selectable(text, false, 14, err ? Ui.Red : me ? Ui.Contrast : Ui.Text);
            var inner = who == null ? (Control)body : Ui.V(5, Ui.Eyebrow(who), body);
            c = new Border
            {
                Background = me ? Ui.BrandHighlight : err ? Ui.RedHighlight : Ui.Raised,
                BorderBrush = me ? Ui.Hex("#661BD96A") : err ? Ui.Hex("#66FF496E") : Ui.Divider,
                BorderThickness = new Thickness(1),
                CornerRadius = me ? new CornerRadius(16, 16, 4, 16) : new CornerRadius(16, 16, 16, 4),
                Padding = new Thickness(14, 10),
                MaxWidth = 640,
                HorizontalAlignment = me ? HorizontalAlignment.Right : HorizontalAlignment.Left,
                Child = inner,
            };
        }
        _msgs.Children.Add(c);
        Dispatcher.UIThread.Post(() => _msgScroll.ScrollToEnd(), DispatcherPriority.Background);
        return c;
    }

    async void Send()
    {
        var text = (_input.Text ?? "").Trim();
        if (text.Length == 0) return;
        _input.Text = "";
        Push("me", text, "you");
        var atts = new JsonArray();
        foreach (var a in _atts) atts.Add(a.DeepClone());
        _atts.Clear();
        RenderAtts();
        _go.IsEnabled = false;
        var thinking = Push("step", "working…");
        try
        {
            var j = await Api.Post("/api/ai/run", new JsonObject { ["instruction"] = text, ["attachments"] = atts });
            _msgs.Children.Remove(thinking);
            foreach (var l in j.A("log").Items()) Push("step", l.S("action") + " - " + l.S("detail"));
            Push("bot", j.S("reply"), "assistant");
            var u = j.N("usage");
            if (u != null) _usage.Text = "tokens " + u.S("total_in") + " in / " + u.S("total_out") + " out";
        }
        catch (Exception e)
        {
            _msgs.Children.Remove(thinking);
            Push("err", e.Message);
        }
        _go.IsEnabled = true;
        _input.Focus();
    }

    async Task Clear()
    {
        await Ops.Act("/api/ai/clear", null, "Chat cleared.");
        _msgs.Children.Clear();
        _intro = Intro();
        _msgs.Children.Add(_intro);
    }

    async void Attach()
    {
        // The engine's own picker runs on the engine's machine, which is this
        // one - but the app can read the file itself and skip tkinter entirely.
        var files = await S.StorageProvider.OpenFilePickerAsync(new Avalonia.Platform.Storage.FilePickerOpenOptions
        {
            Title = "Attach a file for the assistant",
            AllowMultiple = false,
        });
        if (files.Count == 0) return;
        try
        {
            await using var s = await files[0].OpenReadAsync();
            using var rd = new StreamReader(s);
            var buf = new char[200_000];
            var n = await rd.ReadBlockAsync(buf, 0, buf.Length);
            _atts.Add(new JsonObject { ["name"] = files[0].Name, ["content"] = new string(buf, 0, n) });
            RenderAtts();
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
    }

    void RenderAtts()
    {
        _attRow.Children.Clear();
        for (var i = 0; i < _atts.Count; i++)
        {
            var idx = i;
            var chip = Ui.Btn(_atts[i].S("name") + "  ×", () =>
            {
                _atts.RemoveAt(idx);
                RenderAtts();
            }, "paperclip", "xs round");
            chip.Margin = new Thickness(0, 0, 6, 0);
            ToolTip.SetTip(chip, "Remove");
            _attRow.Children.Add(chip);
        }
        _attRow.IsVisible = _atts.Count > 0;
    }
}
