using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Lodestone.Desktop.Core;

namespace Lodestone.Desktop.UI;

/// <summary>
/// Choosing an instance's icon from Minecraft's own items and blocks - about a
/// thousand of them, in groups, with a search. Each group arrives as one picture
/// (a sheet the engine draws), cut into tiles here, so opening "Blocks" is one
/// request rather than three hundred.
/// </summary>
public static class IconPicker
{
    const int Cell = 48;          // what the engine draws each icon at on a sheet
    const int Columns = 16;       // and how many it puts on a row

    static readonly Dictionary<string, Task<Bitmap?>> Sheets = new();

    /// <summary>The key picked, "" for automatic, or null when cancelled.</summary>
    public static async Task<string?> Pick(string current, string autoKey)
    {
        JsonNode cat;
        try { cat = await Api.Get("/api/art/icons"); }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
            return null;
        }
        if (!cat.B("ready"))
        {
            var busy = cat.B("busy");
            var text = busy
                ? "Lodestone is fetching Minecraft's item and block textures from Mojang right now - it takes a few seconds. Try again in a moment."
                : "The item and block textures are not here yet. Lodestone reads them from Mojang's official game download (about half a megabyte)." +
                  (cat.S("error").Length > 0 ? "\n\nLast try: " + cat.S("error") : "");
            var r = await Dialogs.Show("Icons are on their way", Ui.T(text, null, 14, Ui.Text, wrap: true),
                ("Close", "", "ghost"), (busy ? "OK" : "Download them", busy ? "" : "fetch", "brand"));
            if (r == "fetch") await Ops.Act("/api/art/fetch", new { force = false }, "Fetching the textures - open the picker again in a few seconds.");
            return null;
        }

        var items = cat.A("items").Items().ToList();
        var byGroup = items.GroupBy(i => i.S("group")).ToDictionary(g => g.Key, g => g.ToList());
        var groups = cat.A("groups").Items().Select(g => g.S("key")).Where(byGroup.ContainsKey).ToList();
        var label = items.FirstOrDefault(i => i.S("key") == current).S("label");

        var grid = new WrapPanel();
        var scroller = new ScrollViewer
        {
            Content = grid, Height = 380,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
        var search = Ui.Input("", "Search " + items.Count + " items and blocks - diamond, piston, creeper…");
        search.Classes.Add("search");
        search.InnerLeftContent = Icons.Make("search", 15, Ui.Secondary).Also(i => i.Margin = new Thickness(10, 0, 0, 0));
        var chips = new WrapPanel();
        var group = groups.Contains(GroupOf(items, current)) ? GroupOf(items, current) : groups.FirstOrDefault() ?? "";
        var status = Ui.Hint("");

        Button Tile(Control pic, string tip, string result, bool on)
        {
            // the outline lives on a frame inside: the ghost style clears a button's own border
            var frame = new Border
            {
                Child = pic, CornerRadius = new CornerRadius(10), BorderThickness = new Thickness(2),
                BorderBrush = on ? Ui.Brand : Brushes.Transparent, Background = on ? Ui.BrandTint : null,
                Width = 52, Height = 52,
            };
            pic.HorizontalAlignment = HorizontalAlignment.Center;
            pic.VerticalAlignment = VerticalAlignment.Center;
            var b = new Button
            {
                Content = frame, Width = 56, Height = 56, MinHeight = 56, Padding = new Thickness(2),
                Margin = new Thickness(0, 0, 6, 6), CornerRadius = new CornerRadius(12),
            };
            b.Classes.Add("ghost");
            ToolTip.SetTip(b, tip);
            b.Click += (_, _) => Dialogs.Close(result);
            return b;
        }

        async Task<Control> Crop(JsonNode item)
        {
            var g = item.S("group");
            var list = byGroup[g];
            var idx = list.FindIndex(x => x.S("key") == item.S("key"));
            if (!Sheets.TryGetValue(g, out var t) || (t.IsCompleted && t.Result == null))
                Sheets[g] = t = LoadSheet(g);
            var sheet = await t;
            if (sheet == null || idx < 0) return Icons.Make("box", 20, Ui.Secondary);
            var rect = new PixelRect(idx % Columns * Cell, idx / Columns * Cell, Cell, Cell);
            var img = new Image { Source = new CroppedBitmap(sheet, rect), Width = 40, Height = 40 };
            RenderOptions.SetBitmapInterpolationMode(img, BitmapInterpolationMode.HighQuality);
            return img;
        }

        async Task Fill(IEnumerable<JsonNode> show, bool withAuto)
        {
            grid.Children.Clear();
            if (withAuto)
                grid.Children.Add(Tile(Ui.V(0, Images.Icon(autoKey, 30, 6, Brushes.Transparent).Also(i => i.HorizontalAlignment = HorizontalAlignment.Center),
                        Ui.T("Auto", null, 10.5, Ui.Secondary).Also(t => t.HorizontalAlignment = HorizontalAlignment.Center)),
                    "Automatic - picked from what the server runs", "auto", current.Length == 0));
            var list = show.ToList();
            var slots = new List<(Button b, JsonNode item)>();
            foreach (var it in list)
            {
                var b = Tile(Icons.Make("box", 18, Ui.Ring), it.S("label"), "pick:" + it.S("key"), it.S("key") == current);
                grid.Children.Add(b);
                slots.Add((b, it));
            }
            status.Text = list.Count == 0 ? "Nothing matches." : Ui.Plural(list.Count, "icon");
            scroller.Offset = new Vector(0, 0);
            foreach (var (b, it) in slots)
            {
                var pic = await Crop(it);
                pic.HorizontalAlignment = HorizontalAlignment.Center;
                pic.VerticalAlignment = VerticalAlignment.Center;
                if (b.Content is Border f) f.Child = pic;
            }
        }

        void PaintChips()
        {
            chips.Children.Clear();
            foreach (var g in groups)
            {
                var k = g;
                chips.Children.Add(Ui.Chip(g, k == group && (search.Text ?? "").Trim().Length == 0, () =>
                {
                    group = k;
                    search.Text = "";
                    PaintChips();
                    _ = Fill(byGroup[k], true);
                }, byGroup[g].Count).Also(c => c.Margin = new Thickness(0, 0, 6, 6)));
            }
        }

        search.TextChanged += (_, _) =>
        {
            var q = (search.Text ?? "").Trim();
            PaintChips();
            if (q.Length == 0)
            {
                _ = Fill(byGroup[group], true);
                return;
            }
            _ = Fill(items.Where(i => i.S("label").Contains(q, StringComparison.OrdinalIgnoreCase)
                                      || i.S("key").Contains(q.Replace(' ', '_'), StringComparison.OrdinalIgnoreCase)).Take(160), false);
        };

        var surprise = Ui.Btn("Surprise me", () =>
        {
            var pool = items.Where(i => i.S("group") is "Blocks" or "Redstone & tech" or "Workstations" or "Ores & minerals" or "Magic & rare").ToList();
            Dialogs.Close("pick:" + pool[Random.Shared.Next(pool.Count)].S("key"));
        }, "sparkles", "sm", "A random item or block");
        var head = Ui.Split(Ui.V(3,
                Ui.T(label.Length > 0 ? "Now: " + label : "Now: automatic", null, 13, Ui.Secondary),
                Ui.Hint("Drawn from Minecraft " + cat.S("version") + "'s own textures.")),
            surprise);
        var body = Ui.V(12, head, search, chips, scroller, status);
        PaintChips();
        _ = Fill(byGroup[group], true);
        var res = await Dialogs.Show("Choose an icon", body, 820, ("Cancel", "", "ghost"));
        return res switch
        {
            "auto" => "",
            { } s when s.StartsWith("pick:", StringComparison.Ordinal) => s[5..],
            _ => null,
        };
    }

    static string GroupOf(List<JsonNode> items, string key) => items.FirstOrDefault(i => i.S("key") == key).S("group");

    static async Task<Bitmap?> LoadSheet(string group)
    {
        var bytes = await Api.Bytes("/api/art/sheet?group=" + Uri.EscapeDataString(group) + "&size=" + Cell);
        if (bytes == null || bytes.Length == 0) return null;
        try
        {
            using var ms = new MemoryStream(bytes);
            return new Bitmap(ms);
        }
        catch (Exception)
        {
            return null;
        }
    }
}
