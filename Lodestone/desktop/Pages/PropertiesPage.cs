using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>How the server looks in the multiplayer list, then server.properties as a form.</summary>
public sealed class PropertiesPage : Page
{
    public PropertiesPage(MainWindow s) : base(s) { }

    public override string Key => "props";
    public override string Title => "Properties";
    public override string Icon => "sliders";

    readonly Dictionary<string, string> _dirty = new();
    List<JsonNode> _props = new();

    TextBox _motd = null!, _max = null!, _rp = null!, _rpSha = null!, _rpPrompt = null!, _filter = null!;
    ComboBox _rpReq = null!;
    TextBlock _motdTitle = null!, _motdPrev = null!;
    Border _motdIcon = null!;
    UniformGrid _grid = null!;

    protected override Control Build()
    {
        // ---- appearance
        _motdIcon = Ui.Rounded(Ui.T("no icon", null, 10, Ui.Secondary).Also(t =>
        {
            t.HorizontalAlignment = HorizontalAlignment.Center;
            t.VerticalAlignment = VerticalAlignment.Center;
        }), 64, 4, Ui.Hex("#0B0B0D"));
        _motdTitle = Ui.T("A Minecraft Server", null, 15, Ui.Hex("#FFFFFF"));
        _motdTitle.FontFamily = Ui.Mono;
        _motdPrev = new TextBlock { FontFamily = Ui.Mono, FontSize = 14, TextWrapping = TextWrapping.Wrap, LineHeight = 20 };
        var serverList = new Border
        {
            // the multiplayer screen's row: dark, square, pixel icon
            Background = Ui.Hex("#0E0E10"),
            BorderBrush = Ui.Hex("#2A2A2E"),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(12),
            Padding = new Thickness(12),
            Child = new Grid
            {
                ColumnDefinitions = new ColumnDefinitions("Auto,*"),
                Children =
                {
                    _motdIcon,
                    Ui.V(4, _motdTitle, _motdPrev).Also(v => { v.Margin = new Thickness(14, 0, 0, 0); Grid.SetColumn(v, 1); }),
                },
            },
        };
        _motd = Ui.Area("", 2, "&aMy server &7| &bnow with colours", true);
        _motd.TextChanged += (_, _) => Motd.Render(_motdPrev, _motd.Text);
        _max = Ui.Input();
        _rp = Ui.Input();
        _rpSha = Ui.Input(mono: true);
        _rpPrompt = Ui.Input();
        _rpReq = Ui.Select(new[] { ("false", "Optional"), ("true", "Required") }, "false");
        var iconField = Ui.Field("Server icon", Ui.H(8,
                Ui.Btn("Upload PNG", UploadIcon, "image", "sm"),
                Ui.Btn("Remove", ClearIcon, "trash", "sm soft-red")),
            "Any PNG, JPG, WebP, GIF or BMP. The middle square is cut out and resized to the 64×64 PNG Minecraft needs.");
        var appearance = Ui.Card("How the server looks", "MOTD, icon and resource pack",
            Ui.Btn("Save", () => _ = SaveAppearance(), "save", "brand"),
            Ui.V(0,
                serverList.Also(b => b.Margin = new Thickness(0, 0, 0, 18)),
                Ui.Field("MOTD", _motd,
                    "Two lines at most in the server list. &a or §a for colours, &#ff8800 or <#ff8800> for hex, \\n for the second line."),
                Ui.Cols(16, iconField, Ui.Field("Max players", _max)),
                Ui.Cols(16, Ui.Field("Resource pack URL", _rp), Ui.Field("Resource pack SHA-1", _rpSha)),
                Ui.Cols(16, Ui.Field("Resource pack prompt", _rpPrompt), Ui.Field("Require it", _rpReq))));

        // ---- server.properties
        _filter = Ui.Input("", "Filter", width: 200);
        _filter.MinHeight = 32;
        _filter.InnerLeftContent = Icons.Make("search", 15, Ui.Secondary).Also(i => i.Margin = new Thickness(10, 0, 0, 0));
        _filter.TextChanged += (_, _) => Render();
        _grid = new UniformGrid { Columns = 3 };
        _grid.SizeChanged += (_, e) =>
        {
            var cols = Math.Max(1, (int)(e.NewSize.Width / 290));
            if (cols != _grid.Columns) _grid.Columns = cols;
        };
        var props = Ui.Card("server.properties", "Restart the server for most of these to take effect",
            Ui.H(8, _filter, Ui.Btn("Reload", () => _ = LoadProps(), "refresh", "sm"),
                Ui.Btn("Save changes", () => _ = SaveProps(), "save", "brand sm")),
            _grid);

        return Ui.Scroll(Ui.V(16, appearance, props), new Thickness(28, 16, 28, 28));
    }

    public override async Task OnShow()
    {
        await LoadProps();
        await LoadAppearance();
    }

    async Task LoadProps()
    {
        _dirty.Clear();
        try
        {
            var j = await Api.Get("/api/properties");
            _props = j.A("items").Items().ToList();
        }
        catch (Exception e)
        {
            _props = new();
            _grid.Children.Clear();
            _grid.Children.Add(Ui.Empty(e.Message, "alert"));
            return;
        }
        Render();
    }

    void Render()
    {
        var f = (_filter.Text ?? "").Trim().ToLowerInvariant();
        _grid.Children.Clear();
        var use = _props.Where(p => f.Length == 0 || p.S("key").Contains(f, StringComparison.Ordinal)).ToList();
        if (use.Count == 0)
        {
            _grid.Children.Add(Ui.Empty(_props.Count == 0 ? "No server.properties yet - start the server once." : "Nothing matches."));
            return;
        }
        foreach (var p in use)
        {
            var key = p.S("key");
            var value = _dirty.TryGetValue(key, out var dv) ? dv : p.S("value");
            var tile = new Border { Margin = new Thickness(0, 0, 10, 10) };
            tile.Classes.Add("row");
            if (_dirty.ContainsKey(key)) tile.Classes.Add("changed");
            Control ctl;
            switch (p.S("type"))
            {
                case "bool":
                    ToggleButton sw = null!;
                    sw = Ui.Switch(value == "true" ? "on" : "off", value == "true", on =>
                    {
                        sw.Content = on ? "on" : "off";
                        Mark(tile, key, on ? "true" : "false");
                    });
                    ctl = sw;
                    break;
                case "enum":
                    ctl = Ui.Select(p.Strings("options").Select(o => (o, o)), value, v => Mark(tile, key, v));
                    break;
                default:
                    var tb = Ui.Input(value, null, mono: true);
                    tb.TextChanged += (_, _) => Mark(tile, key, tb.Text ?? "");
                    ctl = tb;
                    break;
            }
            var k = Ui.MonoText(key, 12.5, Ui.Contrast);
            k.TextTrimming = TextTrimming.CharacterEllipsis;
            ToolTip.SetTip(k, key);
            tile.Child = Ui.V(8, k, ctl);
            _grid.Children.Add(tile);
        }
    }

    void Mark(Border tile, string key, string value)
    {
        _dirty[key] = value;
        tile.Classes.Set("changed", true);
    }

    async Task SaveProps()
    {
        if (_dirty.Count == 0)
        {
            Toasts.Show("Nothing changed.");
            return;
        }
        var values = new JsonObject();
        foreach (var (k, v) in _dirty) values[k] = v;
        if (await Ops.Act("/api/properties", new JsonObject { ["values"] = values }, "Saved.") != null)
            await LoadProps();
    }

    async Task LoadAppearance()
    {
        try
        {
            var j = await Api.Get("/api/appearance");
            var v = j.N("values");
            _motd.Text = v.S("motd");
            _max.Text = v.S("max-players");
            _rp.Text = v.S("resource-pack");
            _rpSha.Text = v.S("resource-pack-sha1");
            _rpPrompt.Text = v.S("resource-pack-prompt");
            Ui.SetVal(_rpReq, v.S("require-resource-pack") == "true" ? "true" : "false");
            _motdTitle.Text = S.State.N("server").S("name", "A Minecraft Server");
            Motd.Render(_motdPrev, _motd.Text);
            if (j.N("icon").B("present"))
            {
                var bmp = await Images.ServerIcon();
                _motdIcon.Child = bmp != null ? Ui.PixelImage(bmp, 64) : Ui.T("no icon", null, 10, Ui.Secondary);
            }
            else
            {
                _motdIcon.Child = Ui.T("no icon", null, 10, Ui.Secondary).Also(t =>
                {
                    t.HorizontalAlignment = HorizontalAlignment.Center;
                    t.VerticalAlignment = VerticalAlignment.Center;
                });
            }
        }
        catch (Exception)
        {
            // the properties card reports the real error
        }
    }

    async Task SaveAppearance()
    {
        var values = new JsonObject
        {
            ["motd"] = _motd.Text ?? "",
            ["max-players"] = _max.Text ?? "",
            ["resource-pack"] = _rp.Text ?? "",
            ["resource-pack-sha1"] = _rpSha.Text ?? "",
            ["resource-pack-prompt"] = _rpPrompt.Text ?? "",
            ["require-resource-pack"] = Ui.Val(_rpReq) ?? "false",
        };
        if (await Ops.Act("/api/appearance", new JsonObject { ["values"] = values }, "Saved - restart for the MOTD to show.") != null)
            await LoadProps();
    }

    async void UploadIcon()
    {
        var files = await S.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
        {
            Title = "Pick an image for the server icon",
            AllowMultiple = false,
            FileTypeFilter = new[]
            {
                new FilePickerFileType("Image") { Patterns = new[] { "*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.bmp" } },
            },
        });
        if (files.Count == 0) return;
        try
        {
            byte[] png;
            await using (var s = await files[0].OpenReadAsync())
            {
                try { png = Images.ToServerIcon(s); }
                catch (Exception) { throw new Exception("That file could not be read as an image."); }
            }
            using var body = new MemoryStream(png);
            var j = await Api.PostRaw("/api/icon", body);
            Toasts.Good("Icon set - resized to 64×64 (" + j.S("size") + ").");
            await LoadAppearance();
            S.RefreshIcon();
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
    }

    async void ClearIcon()
    {
        if (!await Dialogs.Confirm("Remove the server icon?", "server-icon.png is deleted from the server folder.", "Remove")) return;
        await Ops.Act("/api/icon/clear", null, "Removed.");
        await LoadAppearance();
        S.RefreshIcon();
    }
}
