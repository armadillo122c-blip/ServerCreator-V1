using Avalonia;
using Avalonia.Animation;
using Avalonia.Animation.Easings;
using Avalonia.Controls;
using Avalonia.Controls.Shapes;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;

namespace Lodestone.Desktop.UI;

/// <summary>
/// The opening: about five seconds while the engine connects. The pixel field
/// sweeps in, the mark pops, the name rises letter by letter, the golem and
/// the cat walk on, and a bar fills. A click skips the wait once the engine
/// has answered; it never hides a connection that is still being made.
/// </summary>
public sealed class Intro
{
    public const int Ms = 5000;

    public Grid View { get; }
    public Task Played => _played.Task;

    readonly TaskCompletionSource _played = new();
    readonly PixelField _field;
    readonly Border _logo;
    readonly List<TextBlock> _letters = new();
    readonly TextBlock _tagline;
    readonly Border _golem, _cat;
    readonly Border _fill;
    readonly TextBlock _status;
    readonly TextBlock _skip;
    const double BarWidth = 300;

    public Intro()
    {
        _field = new PixelField
        {
            Start = 1,
            Strength = 0,
            Cell = 6,
            Dim = Color.Parse("#2C2940"),
            Bright = Color.Parse("#A79EDC"),
            Speed = 0.8,
        };
        var vignette = new Border
        {
            Background = new RadialGradientBrush
            {
                GradientStops =
                {
                    new GradientStop(Color.Parse("#F216181C"), 0),
                    new GradientStop(Color.Parse("#B316181C"), 0.55),
                    new GradientStop(Color.Parse("#0016181C"), 1),
                },
            },
        };

        var glow = new Ellipse
        {
            Width = 300,
            Height = 300,
            Fill = new RadialGradientBrush
            {
                GradientStops =
                {
                    new GradientStop(Color.Parse("#661BD96A"), 0),
                    new GradientStop(Color.Parse("#001BD96A"), 1),
                },
            },
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        };
        glow.Classes.Add("glow");
        _logo = new Border { Child = Logo.Make(108), HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
        Hide(_logo, "scale(0.3) rotate(-12deg)");
        var mark = new Grid { Height = 170 };
        mark.Children.Add(glow);
        mark.Children.Add(_logo);

        var word = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Center };
        foreach (var ch in "Lodestone")
        {
            var t = Ui.T(ch.ToString(), null, 48, Ui.Contrast, FontWeight.ExtraBold);
            Hide(t, "translateY(22px)");
            _letters.Add(t);
            word.Children.Add(t);
        }
        _tagline = Ui.T("One panel for every Minecraft server you run", null, 15.5, Ui.Secondary);
        _tagline.HorizontalAlignment = HorizontalAlignment.Center;
        Hide(_tagline, "translateY(8px)");

        _golem = Walker("golem", 92, "translateX(-220px)", "bob");
        _cat = Walker("cat", 60, "translateX(220px)", "bob-slow");
        var pets = Ui.H(26, _golem, _cat);
        pets.HorizontalAlignment = HorizontalAlignment.Center;
        foreach (var p in pets.Children) p.VerticalAlignment = VerticalAlignment.Bottom;
        pets.Height = 100;

        _fill = new Border
        {
            Width = 0,
            Height = 4,
            CornerRadius = new CornerRadius(2),
            HorizontalAlignment = HorizontalAlignment.Left,
            Background = new LinearGradientBrush
            {
                StartPoint = new RelativePoint(0, 0, RelativeUnit.Relative),
                EndPoint = new RelativePoint(1, 0, RelativeUnit.Relative),
                GradientStops = { new GradientStop(Color.Parse("#1BD96A"), 0), new GradientStop(Color.Parse("#A79EDC"), 1) },
            },
        };
        var track = new Border
        {
            Width = BarWidth,
            Height = 4,
            CornerRadius = new CornerRadius(2),
            Background = Ui.ButtonBg,
            Child = _fill,
            HorizontalAlignment = HorizontalAlignment.Center,
        };
        _status = Ui.T("Starting…", null, 13, Ui.Secondary);
        _status.HorizontalAlignment = HorizontalAlignment.Center;

        var center = Ui.V(14, mark, word, _tagline, pets, track, _status);
        center.HorizontalAlignment = HorizontalAlignment.Center;
        center.VerticalAlignment = VerticalAlignment.Center;

        _skip = Ui.T("click to skip", null, 12, Ui.Hex("#5F6873"));
        _skip.HorizontalAlignment = HorizontalAlignment.Center;
        _skip.VerticalAlignment = VerticalAlignment.Bottom;
        _skip.Margin = new Thickness(0, 0, 0, 22);
        _skip.Opacity = 0;
        _skip.Transitions = new Transitions { new DoubleTransition { Property = Visual.OpacityProperty, Duration = TimeSpan.FromMilliseconds(400) } };

        View = new Grid { Background = Ui.Bg, Cursor = new Avalonia.Input.Cursor(Avalonia.Input.StandardCursorType.Hand) };
        View.Children.Add(_field);
        View.Children.Add(vignette);
        View.Children.Add(center);
        View.Children.Add(_skip);
        View.PointerPressed += (_, e) =>
        {
            e.Handled = true;
            _played.TrySetResult();
        };
        View.Transitions = new Transitions
        {
            new DoubleTransition { Property = Visual.OpacityProperty, Duration = TimeSpan.FromMilliseconds(520), Easing = new CubicEaseIn() },
            new TransformOperationsTransition { Property = Visual.RenderTransformProperty, Duration = TimeSpan.FromMilliseconds(520), Easing = new CubicEaseIn() },
        };
    }

    static void Hide(Control c, string from)
    {
        c.Opacity = 0;
        c.RenderTransform = Anim.Tf(from);
    }

    static Border Walker(string mascot, double height, string from, string loop)
    {
        var fig = Art.Figure(mascot, height);
        fig.RenderTransform = new TranslateTransform();
        fig.Classes.Add(loop);
        var wrap = new Border { Child = fig };
        Hide(wrap, from);
        return wrap;
    }

    static void Show(Control c, int ms, Easing easing, string to = "scale(1)")
    {
        c.Transitions = new Transitions
        {
            new DoubleTransition { Property = Visual.OpacityProperty, Duration = TimeSpan.FromMilliseconds(Math.Min(ms, 450)) },
            new TransformOperationsTransition { Property = Visual.RenderTransformProperty, Duration = TimeSpan.FromMilliseconds(ms), Easing = easing },
        };
        c.Opacity = 1;
        c.RenderTransform = Anim.Tf(to);
    }

    static void At(int ms, Action a) => DispatcherTimer.RunOnce(a, TimeSpan.FromMilliseconds(ms));

    public void Play()
    {
        // the dots sweep in from the right and settle
        var t0 = DateTime.UtcNow;
        DispatcherTimer.Run(() =>
        {
            var k = Math.Min(1, (DateTime.UtcNow - t0).TotalMilliseconds / 1600);
            var e = 1 - Math.Pow(1 - k, 3);
            _field.Start = 1 - e * 1.05;
            _field.Strength = 0.85 * e;
            _field.InvalidateVisual();
            return k < 1;
        }, TimeSpan.FromMilliseconds(16));

        At(80, () =>
        {
            _fill.Transitions = new Transitions
            {
                new DoubleTransition { Property = Layoutable.WidthProperty, Duration = TimeSpan.FromMilliseconds(Ms - 300), Easing = new SineEaseInOut() },
            };
            _fill.Width = BarWidth;
        });
        At(250, () =>
        {
            Show(_logo, 700, new BackEaseOut(), "scale(1) rotate(0deg)");
            Sounds.Play("intro");
        });
        for (var i = 0; i < _letters.Count; i++)
        {
            var t = _letters[i];
            At(950 + i * 75, () => Show(t, 520, new BackEaseOut(), "translateY(0px)"));
        }
        At(1900, () => Show(_tagline, 500, new CubicEaseOut(), "translateY(0px)"));
        At(2350, () => Show(_golem, 900, new CubicEaseOut(), "translateX(0px)"));
        At(2550, () => Show(_cat, 900, new CubicEaseOut(), "translateX(0px)"));
        At(2200, () => _skip.Opacity = 1);
        At(Ms, () => _played.TrySetResult());
    }

    public void Status(string msg) => _status.Text = msg;

    /// <summary>Fade out, grow slightly, then leave the tree.</summary>
    public async Task Close(Panel parent)
    {
        View.IsHitTestVisible = false;
        View.Opacity = 0;
        View.RenderTransform = Anim.Tf("scale(1.04)");
        await Task.Delay(560);
        parent.Children.Remove(View);
    }
}
