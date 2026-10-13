using Avalonia;
using Avalonia.Animation;
using Avalonia.Animation.Easings;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Media.Transformation;
using Avalonia.Platform;
using Avalonia.Threading;

namespace Lodestone.Desktop.UI;

/// <summary>
/// The pictures the app ships (Assets/art): scenes for instance and profile
/// banners, and the two mascots that keep watch on the hero banners. Instance
/// icons are Minecraft items and blocks now (Images.Icon). Each is decoded once.
/// </summary>
public static class Art
{
    public static readonly (string key, string label, string file)[] Banners =
    {
        ("forest", "Taiga sunset", "forest.webp"),
        ("valley", "Golden valley", "valley.png"),
        ("jungle", "Jungle morning", "jungle.webp"),
        ("ocean", "Ocean ruins", "ocean.png"),
        ("deepdark", "Deep dark", "deepdark.webp"),
        ("abyss", "Violet depths", "abyss.webp"),
        ("battlefield", "Battlefield", "battlefield.png"),
    };

    /// <summary>The mascots on the heroes: key, name and the whole figure.</summary>
    public static readonly (string key, string label, string figure)[] Mascots =
    {
        ("golem", "Copper golem", "golem.png"),
        ("cat", "Calico cat", "cat.png"),
    };

    static readonly Dictionary<string, Bitmap?> Cache = new();

    public static Bitmap? Load(string file, int? width = null)
    {
        var key = file + "@" + width;
        if (Cache.TryGetValue(key, out var b)) return b;
        try
        {
            using var s = AssetLoader.Open(new Uri("avares://Lodestone/Assets/art/" + file));
            b = width == null ? new Bitmap(s) : Bitmap.DecodeToWidth(s, width.Value, BitmapInterpolationMode.HighQuality);
        }
        catch (Exception)
        {
            b = null;
        }
        Cache[key] = b;
        return b;
    }

    static int Hash(string s)
    {
        var h = 17;
        foreach (var c in s) h = unchecked(h * 31 + c);
        return h & 0x7fffffff;
    }

    /// <summary>The chosen banner, or one picked from the seed so every instance keeps the same one.</summary>
    public static string BannerKey(string? chosen, string seed) =>
        Banners.Any(x => x.key == chosen) ? chosen! : Banners[Hash(seed) % Banners.Length].key;

    public static string BannerFile(string key) =>
        Banners.FirstOrDefault(x => x.key == key).file ?? Banners[0].file;

    /// <summary>A scene that fills its box, cropped. fadeTo blends its bottom into the surface below.</summary>
    public static Border Banner(string key, double height, CornerRadius radius, IBrush? fadeTo, out Image image)
    {
        image = new Image
        {
            Source = Load(BannerFile(key), 1100),
            Stretch = Stretch.UniformToFill,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        };
        RenderOptions.SetBitmapInterpolationMode(image, BitmapInterpolationMode.HighQuality);
        var g = new Grid();
        g.Children.Add(image);
        if (fadeTo is ISolidColorBrush sb)
        {
            var c = sb.Color;
            g.Children.Add(new Border { Background = Fade(Color.FromArgb(0, c.R, c.G, c.B), c, 0.25, true) });
        }
        return new Border { Height = height, CornerRadius = radius, ClipToBounds = true, Child = g };
    }

    public static Border Banner(string key, double height, CornerRadius radius, IBrush? fadeTo = null) =>
        Banner(key, height, radius, fadeTo, out _);

    /// <summary>Top-to-bottom (or left-to-right) two-colour gradient, starting at 'from' (0..1).</summary>
    public static LinearGradientBrush Fade(Color a, Color b, double from = 0, bool vertical = false) => new()
    {
        StartPoint = new RelativePoint(0, 0, RelativeUnit.Relative),
        EndPoint = vertical ? new RelativePoint(0, 1, RelativeUnit.Relative) : new RelativePoint(1, 0, RelativeUnit.Relative),
        GradientStops = { new GradientStop(a, from), new GradientStop(b, 1) },
    };

    /// <summary>A mascot without a tile, at its own proportions - for heroes and empty states.</summary>
    public static Image Figure(string key, double height)
    {
        var file = Mascots.FirstOrDefault(x => x.key == key).figure ?? "golem.png";
        var img = new Image { Source = Load(file), Height = height, Stretch = Stretch.Uniform };
        RenderOptions.SetBitmapInterpolationMode(img, BitmapInterpolationMode.HighQuality);
        return img;
    }
}

/// <summary>
/// Motion, done with property transitions so it is cheap and interruptible:
/// things rise into place, lift under the pointer, pages fade in. Off for
/// --shots, where frames are captured rather than played.
/// </summary>
public static class Anim
{
    public static bool Enabled { get; set; } = true;

    public static Transitions Std(int ms = 260) => new()
    {
        new DoubleTransition { Property = Visual.OpacityProperty, Duration = TimeSpan.FromMilliseconds(ms), Easing = new CubicEaseOut() },
        new TransformOperationsTransition { Property = Visual.RenderTransformProperty, Duration = TimeSpan.FromMilliseconds(ms), Easing = new CubicEaseOut() },
        new BrushTransition { Property = Border.BorderBrushProperty, Duration = TimeSpan.FromMilliseconds(160) },
    };

    public static ITransform Tf(string ops) => TransformOperations.Parse(ops);

    /// <summary>Fade and rise into place once the control is on screen, after delayMs.</summary>
    public static T Enter<T>(T c, int delayMs = 0, double rise = 16) where T : Control
    {
        if (!Enabled) return c;
        c.Opacity = 0;
        c.RenderTransform = Tf("translateY(" + rise + "px)");
        c.Transitions = Std(420);
        var done = false;
        c.AttachedToVisualTree += (_, _) =>
        {
            if (done) return;
            done = true;
            DispatcherTimer.RunOnce(() =>
            {
                c.Opacity = 1;
                c.RenderTransform = Tf("translateY(0px)");
            }, TimeSpan.FromMilliseconds(30 + delayMs));
        };
        return c;
    }

    /// <summary>Lift under the pointer; the border can light up too.</summary>
    public static T Lift<T>(T c, double px = 4, IBrush? hoverBorder = null) where T : Control
    {
        if (!Enabled) return c;
        c.Transitions ??= Std(200);
        IBrush? rest = null;
        c.PointerEntered += (_, _) =>
        {
            c.RenderTransform = Tf("translateY(-" + px + "px)");
            if (hoverBorder != null && c is Border b)
            {
                rest ??= b.BorderBrush;
                b.BorderBrush = hoverBorder;
            }
        };
        c.PointerExited += (_, _) =>
        {
            c.RenderTransform = Tf("translateY(0px)");
            if (hoverBorder != null && c is Border b && rest != null) b.BorderBrush = rest;
        };
        return c;
    }

    /// <summary>While the pointer is over trigger, target takes 'on' (a transform), else 'off'.</summary>
    public static void HoverTransform(Control trigger, Control target, string on, string off = "scale(1)", int ms = 600)
    {
        if (!Enabled) return;
        target.RenderTransform = Tf(off);
        target.Transitions = new Transitions
        {
            new TransformOperationsTransition { Property = Visual.RenderTransformProperty, Duration = TimeSpan.FromMilliseconds(ms), Easing = new CubicEaseOut() },
        };
        trigger.PointerEntered += (_, _) => target.RenderTransform = Tf(on);
        trigger.PointerExited += (_, _) => target.RenderTransform = Tf(off);
    }

    /// <summary>Fade in from an offset after delayMs, for controls already on screen.</summary>
    public static void Reveal(Control c, int delayMs, string from = "translateX(-14px)", string to = "translateX(0px)")
    {
        if (!Enabled) return;
        c.Transitions = null;
        c.Opacity = 0;
        c.RenderTransform = Tf(from);
        c.Transitions = Std(420);
        DispatcherTimer.RunOnce(() =>
        {
            c.Opacity = 1;
            c.RenderTransform = Tf(to);
        }, TimeSpan.FromMilliseconds(16 + delayMs));
    }

    /// <summary>A page arriving: a short fade and rise.</summary>
    public static void PageIn(Control page)
    {
        if (!Enabled) return;
        page.Transitions = null;
        page.Opacity = 0;
        page.RenderTransform = Tf("translateY(10px)");
        page.Transitions = Std(260);
        Dispatcher.UIThread.Post(() =>
        {
            page.Opacity = 1;
            page.RenderTransform = Tf("translateY(0px)");
        }, DispatcherPriority.Background);
    }

    /// <summary>Pop in from a slightly smaller size - dialogs.</summary>
    public static void Pop(Control c)
    {
        if (!Enabled) return;
        c.Transitions = null;
        c.Opacity = 0;
        c.RenderTransform = Tf("scale(0.94)");
        c.Transitions = new Transitions
        {
            new DoubleTransition { Property = Visual.OpacityProperty, Duration = TimeSpan.FromMilliseconds(180) },
            new TransformOperationsTransition { Property = Visual.RenderTransformProperty, Duration = TimeSpan.FromMilliseconds(260), Easing = new BackEaseOut() },
        };
        Dispatcher.UIThread.Post(() =>
        {
            c.Opacity = 1;
            c.RenderTransform = Tf("scale(1)");
        }, DispatcherPriority.Background);
    }

    public static void FadeIn(Control c, int ms = 200)
    {
        if (!Enabled) return;
        c.Transitions = null;
        c.Opacity = 0;
        c.Transitions = new Transitions { new DoubleTransition { Property = Visual.OpacityProperty, Duration = TimeSpan.FromMilliseconds(ms) } };
        Dispatcher.UIThread.Post(() => c.Opacity = 1, DispatcherPriority.Background);
    }
}

/// <summary>
/// A field of small square dots, ordered-dithered so it thins out into a
/// gradient, twinkling and swept by a slow wave. Behind the title bar it
/// brightens towards the window buttons; the intro uses it full screen.
/// </summary>
public sealed class PixelField : Control
{
    /// <summary>Dot pitch in pixels.</summary>
    public double Cell { get; set; } = 5;

    /// <summary>Where the gradient starts, 0..1 across the width; below it there are no dots.</summary>
    public double Start { get; set; } = 0.3;

    /// <summary>Overall strength, 0..1. The intro raises it to sweep the field in.</summary>
    public double Strength { get; set; } = 1;

    /// <summary>Vertical fade: true thins the dots towards the top and bottom edges.</summary>
    public bool EdgeFade { get; set; }

    public Color Dim { get; set; } = Color.Parse("#3A3650");
    public Color Bright { get; set; } = Color.Parse("#C9C0F2");
    public double Speed { get; set; } = 1;

    /// <summary>
    /// When set, dots take their colour from this gradient instead of Dim..Bright:
    /// it is laid across the width PaletteSpan times and flows sideways at
    /// FlowSpeed (gradient widths per second), so the colours travel.
    /// </summary>
    public Color[]? Palette { get; set; }
    public double PaletteSpan { get; set; } = 1.2;
    public double FlowSpeed { get; set; } = 0.06;

    /// <summary>Adds to every dot's strength; above 0 the field fills in evenly.</summary>
    public double Density { get; set; }

    /// <summary>Square dots fill the cell less this many pixels.</summary>
    public double Gap { get; set; } = 1;

    /// <summary>Frame interval while animating.</summary>
    public int FrameMs { get; set; } = 70;

    public static readonly Color[] Aurora =
    {
        Color.Parse("#1BD96A"), Color.Parse("#2CE3D2"), Color.Parse("#4F9CFF"), Color.Parse("#8E6CFF"),
        Color.Parse("#E26CFF"), Color.Parse("#FF6C9E"), Color.Parse("#FFA347"), Color.Parse("#1BD96A"),
    };

    static readonly int[,] Bayer = { { 0, 8, 2, 10 }, { 12, 4, 14, 6 }, { 3, 11, 1, 9 }, { 15, 7, 13, 5 } };
    const int Levels = 24;
    const int Hues = 64, Shades = 8;
    IBrush[]? _paletteBrushes;
    Color[]? _builtFor;

    readonly Random _rnd = new(1234);
    float[] _phase = Array.Empty<float>();
    IBrush[]? _brushes;
    Color _bDim, _bBright;
    DispatcherTimer? _timer;
    readonly DateTime _t0 = DateTime.UtcNow;

    public PixelField()
    {
        IsHitTestVisible = false;
    }

    protected override void OnAttachedToVisualTree(VisualTreeAttachmentEventArgs e)
    {
        base.OnAttachedToVisualTree(e);
        if (!Anim.Enabled) return;
        _timer = new DispatcherTimer(TimeSpan.FromMilliseconds(FrameMs), DispatcherPriority.Render, (_, _) =>
        {
            // Development Performance Mode turns animation off while the window is open
            if (IsEffectivelyVisible && Anim.Enabled) InvalidateVisual();
        });
        _timer.Start();
    }

    protected override void OnDetachedFromVisualTree(VisualTreeAttachmentEventArgs e)
    {
        base.OnDetachedFromVisualTree(e);
        _timer?.Stop();
        _timer = null;
    }

    IBrush[] Brushes()
    {
        if (_brushes != null && _bDim == Dim && _bBright == Bright) return _brushes;
        _bDim = Dim;
        _bBright = Bright;
        _brushes = new IBrush[Levels];
        for (var i = 0; i < Levels; i++)
        {
            var k = i / (double)(Levels - 1);
            byte L(byte a, byte b) => (byte)(a + (b - a) * k);
            var a = (byte)(90 + 165 * k);
            _brushes[i] = new SolidColorBrush(Color.FromArgb(a, L(Dim.R, Bright.R), L(Dim.G, Bright.G), L(Dim.B, Bright.B))).ToImmutable();
        }
        return _brushes;
    }

    /// <summary>Hues x Shades brushes sampled from the palette; shade raises brightness and alpha.</summary>
    IBrush[] PaletteBrushes(Color[] pal)
    {
        if (_paletteBrushes != null && ReferenceEquals(_builtFor, pal)) return _paletteBrushes;
        _builtFor = pal;
        _paletteBrushes = new IBrush[Hues * Shades];
        for (var h = 0; h < Hues; h++)
        {
            var pos = h / (double)Hues * (pal.Length - 1);
            var i = (int)pos;
            var f = pos - i;
            var a = pal[i];
            var b = pal[Math.Min(i + 1, pal.Length - 1)];
            double R = a.R + (b.R - a.R) * f, G = a.G + (b.G - a.G) * f, B = a.B + (b.B - a.B) * f;
            for (var s = 0; s < Shades; s++)
            {
                var k = s / (double)(Shades - 1);
                // dim dots sit back towards the frame colour; bright ones lift towards white
                var lift = Math.Max(0, k - 0.75) * 1.6;
                byte C(double v) => (byte)Math.Clamp(v * (0.55 + 0.45 * k) + (255 - v) * lift, 0, 255);
                var alpha = (byte)(70 + 185 * k);
                _paletteBrushes[h * Shades + s] = new SolidColorBrush(Color.FromArgb(alpha, C(R), C(G), C(B))).ToImmutable();
            }
        }
        return _paletteBrushes;
    }

    public override void Render(DrawingContext ctx)
    {
        var w = Bounds.Width;
        var h = Bounds.Height;
        if (w <= 0 || h <= 0 || Strength <= 0) return;
        var t = Anim.Enabled ? (DateTime.UtcNow - _t0).TotalSeconds * Speed : 0;
        var cols = (int)(w / Cell) + 1;
        var rows = (int)(h / Cell) + 1;
        if (_phase.Length != cols * rows)
        {
            _phase = new float[cols * rows];
            for (var i = 0; i < _phase.Length; i++) _phase[i] = (float)(_rnd.NextDouble() * Math.PI * 2);
        }
        var pal = Palette;
        var brushes = pal == null ? Brushes() : PaletteBrushes(pal);
        var flow = t * FlowSpeed;
        var span = Math.Max(0.0001, 1 - Start);
        for (var y = 0; y < rows; y++)
        {
            var edge = 1.0;
            if (EdgeFade)
            {
                var vy = y / (double)Math.Max(1, rows - 1);
                edge = 1 - Math.Pow(Math.Abs(vy - 0.5) * 2, 3);
            }
            for (var x = 0; x < cols; x++)
            {
                var u = x / (double)cols;
                var g = (u - Start) / span;
                if (g <= 0) continue;
                if (g > 1) g = 1;
                g = g * g * (3 - 2 * g);
                var wave = 0.5 + 0.5 * Math.Sin(u * 10 - t * 1.4 + y * 0.45);
                var twinkle = 0.5 + 0.5 * Math.Sin(t * 2.4 + _phase[y * cols + x]);
                var v = (g * (0.72 + 0.28 * wave) * (0.62 + 0.38 * twinkle) + Density * (0.6 + 0.4 * twinkle)) * Strength * edge;
                var thr = (Bayer[y & 3, x & 3] + 0.5) / 16.0;
                if (v < thr) continue;
                var k = Math.Clamp((v - thr) / (1 - thr + 0.0001) * 0.6 + v * 0.4, 0, 1);
                var size = Math.Max(1, k > 0.7 ? Cell - Gap : Cell - Gap - 1);
                IBrush brush;
                if (pal == null)
                    brush = brushes[(int)(k * (Levels - 1))];
                else
                {
                    var hp = u * PaletteSpan - flow + y * 0.004;
                    hp -= Math.Floor(hp);
                    brush = brushes[(int)(hp * Hues) % Hues * Shades + (int)(k * (Shades - 1))];
                }
                ctx.FillRectangle(brush, new Rect(x * Cell, y * Cell, size, size));
            }
        }
    }
}
