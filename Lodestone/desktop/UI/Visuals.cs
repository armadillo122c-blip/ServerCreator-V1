using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Controls.Shapes;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Lodestone.Desktop.Core;

namespace Lodestone.Desktop.UI;

/// <summary>The Lodestone mark, drawn from the same pixel rectangles as the .ico.</summary>
public static class Logo
{
    static readonly (int x, int y, int w, int h, string c)[] Rects =
    {
        (2, 2, 28, 28, "#2f3140"), (4, 4, 24, 24, "#4a4d63"), (6, 6, 20, 20, "#23242f"),
        (4, 4, 24, 2, "#6b6f8a"), (4, 26, 24, 2, "#1b1c25"), (8, 8, 2, 2, "#3a3c4d"),
        (22, 8, 2, 2, "#3a3c4d"), (8, 22, 2, 2, "#3a3c4d"), (22, 22, 2, 2, "#3a3c4d"),
        (14, 9, 4, 14, "#123524"), (9, 14, 14, 4, "#123524"), (15, 10, 2, 12, "#22c55e"),
        (10, 15, 12, 2, "#22c55e"), (15, 10, 2, 4, "#e8fff2"), (14, 14, 4, 4, "#4ade80"),
        (15, 15, 2, 2, "#eafff3"),
    };

    public static Control Make(double size)
    {
        var c = new Canvas { Width = 32, Height = 32 };
        foreach (var r in Rects)
        {
            var rect = new Rectangle { Width = r.w, Height = r.h, Fill = Ui.Hex(r.c) };
            Canvas.SetLeft(rect, r.x);
            Canvas.SetTop(rect, r.y);
            c.Children.Add(rect);
        }
        RenderOptions.SetEdgeMode(c, EdgeMode.Aliased);
        return new Viewbox { Width = size, Height = size, Child = c };
    }
}

/// <summary>What a server's picture is made from, as the shelf listing reports it.</summary>
public readonly record struct ServerLook(string Id, bool Local, bool HasIcon, string Software, string Icon)
{
    public static ServerLook From(System.Text.Json.Nodes.JsonNode? s) =>
        new(s.S("id"), s.S("kind", "local") == "local", s.B("has_icon"), s.S("software"), s.S("icon"));
}

/// <summary>Player heads and the server icon, fetched once and kept.</summary>
public static class Images
{
    static readonly Dictionary<string, Task<Bitmap?>> Heads = new();

    public static Task<Bitmap?> Head(string player)
    {
        if (!Heads.TryGetValue(player, out var t))
            Heads[player] = t = Load(() => Api.External("https://minotar.net/helm/" + Uri.EscapeDataString(player) + "/32.png"));
        return t;
    }

    public static Task<Bitmap?> ServerIcon() => Load(() => Api.Bytes("/api/icon?t=" + DateTime.UtcNow.Ticks));

    /// <summary>
    /// Any picture as the 64×64 PNG Minecraft wants: the centre square is cut
    /// out so nothing is stretched, then scaled. Small pixel art is enlarged
    /// with hard edges; bigger pictures are shrunk smoothly.
    /// </summary>
    public static byte[] ToServerIcon(Stream input)
    {
        using var src = new Bitmap(input);
        var w = src.PixelSize.Width;
        var h = src.PixelSize.Height;
        var side = Math.Min(w, h);
        var crop = new Rect((w - side) / 2.0, (h - side) / 2.0, side, side);
        using var rt = new RenderTargetBitmap(new PixelSize(64, 64), new Vector(96, 96));
        using (var ctx = rt.CreateDrawingContext())
        using (ctx.PushRenderOptions(new RenderOptions
               {
                   BitmapInterpolationMode = side < 64 ? BitmapInterpolationMode.None : BitmapInterpolationMode.HighQuality,
               }))
            ctx.DrawImage(src, crop, new Rect(0, 0, 64, 64));
        using var ms = new MemoryStream();
        rt.Save(ms);
        return ms.ToArray();
    }

    static async Task<Bitmap?> Load(Func<Task<byte[]?>> fetch)
    {
        var bytes = await fetch();
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

    /// <summary>A rounded image slot that fills itself in when the image arrives.</summary>
    public static Border Slot(Task<Bitmap?> image, double size, double radius, string fallbackIcon = "user")
    {
        var slot = Ui.Rounded(Icons.Make(fallbackIcon, size * 0.5, Ui.Secondary), size, radius);
        _ = Fill(slot, image, size);
        return slot;
    }

    static async Task Fill(Border slot, Task<Bitmap?> image, double size)
    {
        var bmp = await image;
        if (bmp != null) slot.Child = Ui.PixelImage(bmp, size);
    }

    static readonly Dictionary<string, Task<Bitmap?>> ArtCache = new();
    static readonly Dictionary<string, Task<Bitmap?>> EngineCache = new();

    /// <summary>
    /// Lodestone's pixel art for a kind of server (grass block, paper, anvil…),
    /// rendered by the engine from art.py. Falls back to a plain glyph.
    /// </summary>
    public static Border Art(string? software, double size, double radius = 12, string fallbackIcon = "box")
    {
        var slot = Ui.Rounded(Icons.Make(fallbackIcon, size * 0.5, Ui.Secondary), size, radius);
        if (string.IsNullOrEmpty(software)) return slot;
        if (!ArtCache.TryGetValue(software, out var t))
            ArtCache[software] = t = Load(() => Api.Bytes("/api/art/" + Uri.EscapeDataString(software) + ".png?size=128"));
        _ = FillArt(slot, t, size);
        return slot;
    }

    static async Task FillArt(Border slot, Task<Bitmap?> image, double size)
    {
        var bmp = await image;
        if (bmp == null) return;
        // a little inset so the art sits in its slot like an item in a frame
        slot.Child = new Border { Padding = new Thickness(size * 0.12), Child = Smooth(bmp, size * 0.76) };
    }

    /// <summary>A picture drawn bigger than its slot, scaled down smoothly.</summary>
    static Image Smooth(Bitmap bmp, double size)
    {
        var img = new Image { Source = bmp, Width = size, Height = size, Stretch = Stretch.Uniform };
        RenderOptions.SetBitmapInterpolationMode(img, BitmapInterpolationMode.HighQuality);
        return img;
    }

    static readonly Dictionary<string, Task<Bitmap?>> IconCache = new();

    /// <summary>
    /// A Minecraft item or block ("item/diamond", "block/grass_block") on a
    /// rounded tile - every instance's icon. The engine draws it from the game's
    /// own textures at twice the size shown, so it stays sharp when scaled.
    /// A failed fetch is not kept: the textures may still be downloading.
    /// </summary>
    public static Border Icon(string? key, double size, double radius = 12, IBrush? bg = null)
    {
        var slot = Ui.Rounded(Icons.Make("box", size * 0.46, Ui.Secondary), size, radius, bg ?? Ui.Hex("#2E3037"));
        if (string.IsNullOrEmpty(key)) return slot;
        var inner = size * 0.8;
        var px = Math.Clamp((int)Math.Ceiling(inner * 2 / 16.0) * 16, 32, 256);
        var path = "/api/art/icon/" + key + ".png?size=" + px;
        if (!IconCache.TryGetValue(path, out var t) || (t.IsCompleted && t.Result == null))
            IconCache[path] = t = Load(() => Api.Bytes(path));
        _ = FillIcon(slot, t, inner);
        return slot;
    }

    static async Task FillIcon(Border slot, Task<Bitmap?> image, double inner)
    {
        var bmp = await image;
        if (bmp == null) return;
        var img = Smooth(bmp, inner);
        img.HorizontalAlignment = Avalonia.Layout.HorizontalAlignment.Center;
        img.VerticalAlignment = Avalonia.Layout.VerticalAlignment.Center;
        slot.Child = img;
    }

    /// <summary>
    /// A server's picture in Lodestone: its item or block icon. An engine too old
    /// to report one falls back to server-icon.png, the art for what it runs,
    /// or the plain local/remote glyph.
    /// </summary>
    public static Border ServerIcon(ServerLook s, double size, double radius = 14)
    {
        // an item or block, picked or suggested by what the server runs
        if (s.Icon.Length > 0) return Icon(s.Icon, size, radius);
        if (s.HasIcon)
        {
            var slot = Ui.Rounded(Icons.Make(s.Local ? "box" : "globe", size * 0.46, Ui.Secondary), size, radius);
            var key = "/api/icon?sid=" + Uri.EscapeDataString(s.Id);
            if (!EngineCache.TryGetValue(key, out var t))
                EngineCache[key] = t = Load(() => Api.Bytes(key));
            _ = Fill(slot, t, size);
            return slot;
        }
        if (s.Software.Length > 0) return Art(s.Software, size, radius);
        return Ui.Rounded(Icons.Make(s.Local ? "box" : "globe", size * 0.46, Ui.Secondary), size, radius);
    }

    /// <summary>An image the engine serves (a mod's icon read out of its jar).</summary>
    public static Border FromEngine(string apiPath, double size, double radius = 10, string fallbackIcon = "puzzle")
    {
        var slot = Ui.Rounded(Icons.Make(fallbackIcon, size * 0.5, Ui.Secondary), size, radius);
        if (!EngineCache.TryGetValue(apiPath, out var t))
            EngineCache[apiPath] = t = Load(() => Api.Bytes(apiPath));
        _ = FillSmooth(slot, t, size);
        return slot;
    }

    /// <summary>Forget cached server icons, e.g. after a new one is uploaded.</summary>
    public static void ForgetServerIcons()
    {
        foreach (var k in EngineCache.Keys.Where(k => k.StartsWith("/api/icon", StringComparison.Ordinal)).ToList())
            EngineCache.Remove(k);
    }

    /// <summary>A screenshot thumbnail: wide, cropped to fill, rounded.</summary>
    public static Border Shot(string url, double width, double height)
    {
        var slot = new Border
        {
            Width = width, Height = height, CornerRadius = new CornerRadius(10), ClipToBounds = true,
            Background = Ui.ButtonBg,
            Child = Icons.Make("image", 22, Ui.Secondary).Also(i =>
            {
                i.HorizontalAlignment = Avalonia.Layout.HorizontalAlignment.Center;
                i.VerticalAlignment = Avalonia.Layout.VerticalAlignment.Center;
            }),
        };
        if (!Remote.TryGetValue(url, out var t))
            Remote[url] = t = Load(() => Api.External(url));
        _ = FillShot(slot, t, width, height);
        return slot;
    }

    static async Task FillShot(Border slot, Task<Bitmap?> image, double w, double h)
    {
        var bmp = await image;
        if (bmp == null) return;
        var img = new Image { Source = bmp, Width = w, Height = h, Stretch = Stretch.UniformToFill };
        RenderOptions.SetBitmapInterpolationMode(img, BitmapInterpolationMode.HighQuality);
        slot.Child = img;
    }

    static readonly Dictionary<string, Task<Bitmap?>> Remote = new();

    /// <summary>A project icon from Modrinth, Hangar or SpigotMC, fetched once and kept.</summary>
    public static Border Project(string? url, double size, double radius = 12, string fallbackIcon = "puzzle")
    {
        var slot = Ui.Rounded(Icons.Make(fallbackIcon, size * 0.5, Ui.Secondary), size, radius);
        if (string.IsNullOrEmpty(url)) return slot;
        if (!Remote.TryGetValue(url, out var t))
            Remote[url] = t = Load(() => Api.External(url));
        _ = FillSmooth(slot, t, size);
        return slot;
    }

    static async Task FillSmooth(Border slot, Task<Bitmap?> image, double size)
    {
        var bmp = await image;
        if (bmp == null) return;
        var img = new Image { Source = bmp, Width = size, Height = size, Stretch = Stretch.UniformToFill };
        RenderOptions.SetBitmapInterpolationMode(img, BitmapInterpolationMode.HighQuality);
        slot.Child = img;
    }
}

/// <summary>
/// MOTD text as the Minecraft server list draws it: &amp; or § codes, stateful
/// until reset, plus &amp;#rrggbb and &lt;#rrggbb&gt; hex colours.
/// </summary>
public static class Motd
{
    static readonly Dictionary<char, string> Colors = new()
    {
        ['0'] = "#000000", ['1'] = "#0000AA", ['2'] = "#00AA00", ['3'] = "#00AAAA", ['4'] = "#AA0000",
        ['5'] = "#AA00AA", ['6'] = "#FFAA00", ['7'] = "#AAAAAA", ['8'] = "#555555", ['9'] = "#5555FF",
        ['a'] = "#55FF55", ['b'] = "#55FFFF", ['c'] = "#FF5555", ['d'] = "#FF55FF", ['e'] = "#FFFF55",
        ['f'] = "#FFFFFF",
    };

    public static void Render(TextBlock target, string? text)
    {
        var inl = new InlineCollection();
        string color = "#AAAAAA";
        bool bold = false, italic = false, under = false, strike = false;
        var buf = new System.Text.StringBuilder();

        void Flush()
        {
            if (buf.Length == 0) return;
            var r = new Run(buf.ToString())
            {
                Foreground = Ui.Hex(color),
                FontWeight = bold ? FontWeight.Bold : FontWeight.Normal,
                FontStyle = italic ? FontStyle.Italic : FontStyle.Normal,
            };
            if (under || strike)
            {
                var d = new TextDecorationCollection();
                if (under) d.AddRange(TextDecorations.Underline);
                if (strike) d.AddRange(TextDecorations.Strikethrough);
                r.TextDecorations = d;
            }
            inl.Add(r);
            buf.Clear();
        }

        var src = (text ?? "").Replace("\\n", "\n");
        for (var i = 0; i < src.Length; i++)
        {
            var ch = src[i];
            if ((ch == '&' || ch == '§') && i + 1 < src.Length)
            {
                var nxt = char.ToLowerInvariant(src[i + 1]);
                if (nxt == '#' && i + 8 <= src.Length && IsHex(src.Substring(i + 2, 6)))
                {
                    Flush(); color = "#" + src.Substring(i + 2, 6); i += 7; continue;
                }
                if (Colors.TryGetValue(nxt, out var c))
                {
                    Flush(); color = c; bold = italic = under = strike = false; i++; continue;
                }
                switch (nxt)
                {
                    case 'l': Flush(); bold = true; i++; continue;
                    case 'o': Flush(); italic = true; i++; continue;
                    case 'n': Flush(); under = true; i++; continue;
                    case 'm': Flush(); strike = true; i++; continue;
                    case 'k': Flush(); i++; continue;
                    case 'r': Flush(); color = "#AAAAAA"; bold = italic = under = strike = false; i++; continue;
                }
            }
            if (ch == '<' && i + 9 <= src.Length && src[i + 1] == '#' && src[i + 8] == '>' && IsHex(src.Substring(i + 2, 6)))
            {
                Flush(); color = src.Substring(i + 1, 7); i += 8; continue;
            }
            buf.Append(ch);
        }
        Flush();
        if (inl.Count == 0) inl.Add(new Run("A Minecraft Server") { Foreground = Ui.Hex("#AAAAAA") });
        target.Inlines = inl;
    }

    static bool IsHex(string s) => s.All(Uri.IsHexDigit);
}
