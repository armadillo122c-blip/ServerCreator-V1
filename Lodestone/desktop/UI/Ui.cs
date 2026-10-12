using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;

namespace Lodestone.Desktop.UI;

/// <summary>
/// Small builders for the Modrinth pieces every page uses - cards, rows, tags,
/// buttons with icons, labelled fields. Pages are built in code (they are
/// mostly lists made from JSON), and these keep that code close to what it
/// draws.
/// </summary>
public static class Ui
{
    // ------------------------------------------------------------ colours

    public static IBrush Res(string key) =>
        Application.Current!.TryFindResource(key, out var v) && v is IBrush b ? b : Brushes.Magenta;

    static readonly Dictionary<string, IBrush> HexCache = new();

    public static IBrush Hex(string hex)
    {
        if (!HexCache.TryGetValue(hex, out var b))
            HexCache[hex] = b = new SolidColorBrush(Color.Parse(hex)).ToImmutable();
        return b;
    }

    public static IBrush Bg => Res("MrBg");
    public static IBrush Frame => Res("MrFrame");
    public static IBrush Raised => Res("MrRaised");
    public static IBrush RowBg => Res("MrRow");
    public static IBrush ButtonBg => Res("MrButton");
    public static IBrush Divider => Res("MrDivider");
    public static IBrush Ring => Res("MrRing");
    public static IBrush Terminal => Res("MrTerminal");
    public static IBrush Text => Res("MrText");
    public static IBrush Secondary => Res("MrSecondary");
    public static IBrush Contrast => Res("MrContrast");
    public static IBrush Brand => Res("MrBrand");
    public static IBrush BrandHighlight => Res("MrBrandHighlight");
    public static IBrush BrandTint => Res("MrBrandTint");
    public static IBrush Red => Res("MrRed");
    public static IBrush RedHighlight => Res("MrRedHighlight");
    public static IBrush Orange => Res("MrOrange");
    public static IBrush OrangeHighlight => Res("MrOrangeHighlight");
    public static IBrush Blue => Res("MrBlue");
    public static IBrush BlueHighlight => Res("MrBlueHighlight");
    public static IBrush Purple => Res("MrPurple");

    public static FontFamily Mono =>
        Application.Current!.TryFindResource("MonoFont", out var v) && v is FontFamily f ? f : FontFamily.Default;

    // ------------------------------------------------------------ text

    public static TextBlock T(string? text, string? cls = null, double? size = null, IBrush? fg = null,
                              FontWeight? weight = null, bool wrap = false)
    {
        var t = new TextBlock { Text = text ?? "" };
        if (!string.IsNullOrEmpty(cls)) t.Classes.AddRange(cls.Split(' ', StringSplitOptions.RemoveEmptyEntries));
        if (size != null) t.FontSize = size.Value;
        if (fg != null) t.Foreground = fg;
        if (weight != null) t.FontWeight = weight.Value;
        if (wrap) t.TextWrapping = TextWrapping.Wrap;
        return t;
    }

    public static TextBlock H1(string t) => T(t, "h1");
    public static TextBlock H2(string t) => T(t, "h2");
    public static TextBlock H3(string t) => T(t, "h3");
    public static TextBlock Hint(string? t) => T(t, "hint");
    public static TextBlock Label(string t) => T(t, "label");
    public static TextBlock Eyebrow(string t) => T(t.ToUpperInvariant(), "eyebrow");

    public static TextBlock MonoText(string? t, double size = 13, IBrush? fg = null) =>
        new() { Text = t ?? "", FontFamily = Mono, FontSize = size, Foreground = fg ?? Text };

    public static SelectableTextBlock Selectable(string? t, bool mono = false, double size = 13, IBrush? fg = null)
    {
        var s = new SelectableTextBlock
        {
            Text = t ?? "",
            FontSize = size,
            TextWrapping = TextWrapping.Wrap,
            Foreground = fg ?? Text,
            SelectionBrush = Hex("#661BD96A"),
        };
        if (mono) s.FontFamily = Mono;
        return s;
    }

    // ------------------------------------------------------------ layout

    public static StackPanel V(double gap, params Control?[] kids)
    {
        var s = new StackPanel { Orientation = Orientation.Vertical, Spacing = gap };
        foreach (var k in kids)
            if (k != null) s.Children.Add(k);
        return s;
    }

    public static StackPanel H(double gap, params Control?[] kids)
    {
        var s = new StackPanel { Orientation = Orientation.Horizontal, Spacing = gap };
        foreach (var k in kids)
            if (k != null)
            {
                if (k.VerticalAlignment == VerticalAlignment.Stretch) k.VerticalAlignment = VerticalAlignment.Center;
                s.Children.Add(k);
            }
        return s;
    }

    /// <summary>Left fills, right hugs: the header row of nearly every card.</summary>
    public static Grid Split(Control left, Control? right, double gap = 12)
    {
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        left.VerticalAlignment = VerticalAlignment.Center;
        g.Children.Add(left);
        if (right != null)
        {
            right.Margin = new Thickness(gap, 0, 0, 0);
            right.VerticalAlignment = VerticalAlignment.Center;
            Grid.SetColumn(right, 1);
            g.Children.Add(right);
        }
        return g;
    }

    /// <summary>Equal-width columns with a gap, like the page's grid2/grid3.</summary>
    public static Grid Cols(double gap, params Control[] kids)
    {
        var g = new Grid();
        for (var i = 0; i < kids.Length; i++)
        {
            g.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Star));
            var k = kids[i];
            k.Margin = new Thickness(i == 0 ? 0 : gap / 2, k.Margin.Top, i == kids.Length - 1 ? 0 : gap / 2, k.Margin.Bottom);
            Grid.SetColumn(k, i);
            g.Children.Add(k);
        }
        return g;
    }

    public static Border Divider1(double margin = 4) => new()
    {
        Height = 1,
        Background = Divider,
        Margin = new Thickness(0, margin),
    };

    public static ScrollViewer Scroll(Control content, Thickness? pad = null) => new()
    {
        HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
        Content = new Border { Padding = pad ?? new Thickness(28, 22, 28, 32), Child = content },
    };

    // ------------------------------------------------------------ buttons

    public static Button Btn(string text, Action? onClick = null, string? icon = null, string cls = "",
                             string? tip = null)
    {
        var b = new Button();
        if (!string.IsNullOrEmpty(cls)) b.Classes.AddRange(cls.Split(' ', StringSplitOptions.RemoveEmptyEntries));
        var small = b.Classes.Contains("xs") || b.Classes.Contains("sm");
        b.Content = icon == null
            ? text
            : H(7, Icons.Make(icon, small ? 15 : 17), new TextBlock { Text = text, VerticalAlignment = VerticalAlignment.Center });
        if (onClick != null) b.Click += (_, _) => onClick();
        if (tip != null) ToolTip.SetTip(b, tip);
        return b;
    }

    public static Button IconBtn(string icon, string tip, Action onClick, string cls = "", double size = 36)
    {
        var b = new Button
        {
            Content = Icons.Make(icon, size <= 30 ? 15 : 18),
            Width = size,
            Height = size,
            MinHeight = size,
            Padding = new Thickness(0),
            HorizontalContentAlignment = HorizontalAlignment.Center,
        };
        b.Classes.Add("square");
        if (!string.IsNullOrEmpty(cls)) b.Classes.AddRange(cls.Split(' ', StringSplitOptions.RemoveEmptyEntries));
        ToolTip.SetTip(b, tip);
        b.Click += (_, _) => onClick();
        return b;
    }

    // ------------------------------------------------------------ surfaces

    public static Border Card(Control child, double pad = 20)
    {
        var b = new Border { Child = child, Padding = new Thickness(pad) };
        b.Classes.Add("card");
        return b;
    }

    /// <summary>Card with a title row: heading, optional sub-line, actions on the right.</summary>
    public static Border Card(string title, string? sub, Control? actions, Control? body)
    {
        var head = V(3, H2(title), sub == null ? null : Hint(sub));
        var col = V(16, Split(head, actions));
        if (body != null) col.Children.Add(body);
        return Card(col);
    }

    /// <summary>A card whose body fills the remaining height (console, editor, chat).</summary>
    public static Border FillCard(Control header, Control body, Thickness? bodyMargin = null)
    {
        var g = new Grid { RowDefinitions = new RowDefinitions("Auto,*") };
        header.Margin = new Thickness(20, 16, 16, 12);
        g.Children.Add(header);
        body.Margin = bodyMargin ?? new Thickness(0);
        Grid.SetRow(body, 1);
        g.Children.Add(body);
        var b = new Border { Child = g, ClipToBounds = true };
        b.Classes.Add("card");
        return b;
    }

    public static Border Row(Control main, params Control?[] actions)
    {
        var right = H(6, actions);
        var b = new Border { Child = Split(main, right.Children.Count > 0 ? right : null) };
        b.Classes.Add("row");
        return b;
    }

    /// <summary>
    /// An icon or avatar beside text that may wrap. A horizontal StackPanel
    /// would hand the text infinite width, so it would never wrap and would
    /// run underneath the row's buttons instead.
    /// </summary>
    public static Grid Lead(Control lead, Control body, double gap = 12)
    {
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*") };
        lead.VerticalAlignment = VerticalAlignment.Center;
        g.Children.Add(lead);
        body.Margin = new Thickness(gap, 0, 0, 0);
        body.VerticalAlignment = VerticalAlignment.Center;
        Grid.SetColumn(body, 1);
        g.Children.Add(body);
        return g;
    }

    /// <summary>A title over a sub-line: the left half of most rows.</summary>
    public static StackPanel TitleSub(string title, string? sub, params Control?[] after)
    {
        var top = H(8, T(title, "h3"));
        foreach (var a in after)
            if (a != null) top.Children.Add(a);
        var s = V(3, top);
        if (!string.IsNullOrEmpty(sub))
        {
            var t = T(sub, null, 12.5, Secondary);
            t.TextWrapping = TextWrapping.Wrap;
            s.Children.Add(t);
        }
        return s;
    }

    public static Border Code(string text, double size = 12.5)
    {
        var b = new Border
        {
            Child = Selectable(text, true, size, Text),
        };
        b.Classes.Add("code");
        return b;
    }

    public static Control Empty(string text, string? icon = null)
    {
        var s = V(10);
        s.HorizontalAlignment = HorizontalAlignment.Center;
        s.Margin = new Thickness(16, 26);
        if (icon != null)
        {
            var i = Icons.Make(icon, 28, Secondary);
            i.HorizontalAlignment = HorizontalAlignment.Center;
            i.Opacity = 0.7;
            s.Children.Add(i);
        }
        var t = T(text, null, 13.5, Secondary);
        t.TextAlignment = TextAlignment.Center;
        t.TextWrapping = TextWrapping.Wrap;
        t.MaxWidth = 520;
        s.Children.Add(t);
        return s;
    }

    public static Grid KRow(string k, Control v)
    {
        var g = new Grid
        {
            ColumnDefinitions = new ColumnDefinitions("Auto,*"),
            Margin = new Thickness(0, 5),
        };
        var kt = T(k, null, 13, Secondary);
        kt.Margin = new Thickness(0, 0, 16, 0);
        kt.VerticalAlignment = VerticalAlignment.Top;
        g.Children.Add(kt);
        v.HorizontalAlignment = HorizontalAlignment.Right;
        Grid.SetColumn(v, 1);
        g.Children.Add(v);
        return g;
    }

    public static Grid KRow(string k, string? v, IBrush? fg = null)
    {
        var t = MonoText(v ?? "—", 12.5, fg ?? Contrast);
        t.TextTrimming = TextTrimming.CharacterEllipsis;
        t.TextAlignment = TextAlignment.Right;
        ToolTip.SetTip(t, v);
        return KRow(k, t);
    }

    // ------------------------------------------------------------ chips

    /// <summary>Modrinth's grey tag: loaders, categories - here, facts about a server.</summary>
    public static Border Tag(string text, string? icon = null, IBrush? fg = null)
    {
        var col = fg ?? Secondary;
        var t = T(text, null, 12.5, col);
        var b = new Border { Child = icon == null ? t : H(5, Icons.Make(icon, 13, col), t) };
        b.Classes.Add("tag");
        return b;
    }

    public static (IBrush fg, IBrush bg) PillColors(string kind) => kind switch
    {
        "on" => (Brand, BrandHighlight),
        "bad" => (Red, RedHighlight),
        "warn" => (Orange, OrangeHighlight),
        "info" => (Blue, BlueHighlight),
        _ => (Secondary, ButtonBg),
    };

    /// <summary>A status pill: coloured dot and label on a tinted background.</summary>
    public static Border Pill(string text, string kind = "", bool dot = true)
    {
        var p = new Border
        {
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(9, 3, 10, 3),
            VerticalAlignment = VerticalAlignment.Center,
        };
        SetPill(p, text, kind, dot);
        return p;
    }

    public static void SetPill(Border p, string text, string kind, bool dot = true)
    {
        var (fg, bg) = PillColors(kind);
        p.Background = bg;
        var label = T(text, null, 12.5, fg, FontWeight.SemiBold);
        p.Child = dot
            ? H(6, new Border { Width = 7, Height = 7, CornerRadius = new CornerRadius(4), Background = fg }, label)
            : label;
    }

    /// <summary>A small stat tile: icon badge, big number, label under it.</summary>
    public static Border Stat(string label, string value, string icon, IBrush? accent = null)
    {
        var badge = new Border
        {
            Width = 36, Height = 36, CornerRadius = new CornerRadius(10),
            Background = ButtonBg,
            Child = Icons.Make(icon, 18, accent ?? Text).Also(i =>
            {
                i.HorizontalAlignment = HorizontalAlignment.Center;
                i.VerticalAlignment = VerticalAlignment.Center;
            }),
        };
        var card = Card(Lead(badge, V(0, T(value, null, 19, accent ?? Contrast, FontWeight.Bold),
            T(label, null, 12.5, Secondary))), 12);
        card.MinWidth = 150;
        return card;
    }

    /// <summary>A filter chip: a small pill that is either on or off, with an optional count.</summary>
    public static Button Chip(string label, bool on, Action onClick, int? count = null)
    {
        var text = count == null ? label : label + "  " + count;
        var b = Btn(text, onClick, null, on ? "brand xs" : "xs");
        b.Margin = new Thickness(0, 0, 6, 6);
        return b;
    }

    // ------------------------------------------------------------ inputs

    public static TextBox Input(string? value = "", string? placeholder = null, bool password = false,
                                bool mono = false, double? width = null)
    {
        var t = new TextBox { Text = value ?? "", Watermark = placeholder };
        if (password) t.PasswordChar = '•';
        if (mono) t.Classes.Add("mono");
        if (width != null) t.Width = width.Value;
        return t;
    }

    public static TextBox Area(string? value, int rows = 3, string? placeholder = null, bool mono = false)
    {
        var t = new TextBox
        {
            Text = value ?? "",
            Watermark = placeholder,
            AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap,
            MinHeight = 22 * rows + 18,
            VerticalContentAlignment = VerticalAlignment.Top,
        };
        if (mono) t.Classes.Add("mono");
        return t;
    }

    public static Control Field(string label, Control input, string? hint = null)
    {
        var s = V(0, Label(label), input);
        if (hint != null)
        {
            var h = Hint(hint);
            h.Margin = new Thickness(0, 6, 0, 0);
            s.Children.Add(h);
        }
        s.Margin = new Thickness(0, 0, 0, 14);
        return s;
    }

    public static ComboBox Select(IEnumerable<(string value, string label)> opts, string? selected,
                                  Action<string>? changed = null, double? width = null)
    {
        var c = new ComboBox();
        var i = 0;
        foreach (var (v, l) in opts)
        {
            if (v.StartsWith("--", StringComparison.Ordinal))
            {
                var hdr = new ComboBoxItem { Content = l.ToUpperInvariant(), IsEnabled = false, Tag = null };
                hdr.Classes.Add("group");
                c.Items.Add(hdr);
            }
            else
            {
                c.Items.Add(new ComboBoxItem { Content = l, Tag = v });
                if (v == selected) c.SelectedIndex = i;
            }
            i++;
        }
        if (c.SelectedIndex < 0)
        {
            for (var k = 0; k < c.Items.Count; k++)
                if (c.Items[k] is ComboBoxItem { Tag: string })
                {
                    c.SelectedIndex = k;
                    break;
                }
        }
        if (width != null)
        {
            c.Width = width.Value;
            c.HorizontalAlignment = HorizontalAlignment.Left;
        }
        if (changed != null)
            c.SelectionChanged += (_, _) =>
            {
                var v = Val(c);
                if (v != null) changed(v);
            };
        return c;
    }

    public static string? Val(ComboBox c) => (c.SelectedItem as ComboBoxItem)?.Tag as string;

    public static void SetVal(ComboBox c, string value)
    {
        for (var k = 0; k < c.Items.Count; k++)
            if (c.Items[k] is ComboBoxItem { Tag: string t } && t == value)
            {
                c.SelectedIndex = k;
                return;
            }
    }

    public static ToggleButton Switch(string? label, bool on, Action<bool>? changed = null)
    {
        var t = new ToggleButton { IsChecked = on, Content = label };
        t.Classes.Add("switch");
        if (changed != null)
            t.IsCheckedChanged += (_, _) => changed(t.IsChecked == true);
        return t;
    }

    /// <summary>A switch with an explanation tucked under its label.</summary>
    public static Control SwitchHint(ToggleButton sw, string? hint)
    {
        if (hint == null)
        {
            sw.Margin = new Thickness(0, 0, 0, 12);
            return sw;
        }
        var h = Hint(hint);
        h.Margin = new Thickness(53, 2, 0, 14);
        return V(0, sw, h);
    }

    public static ToggleButton Tile(string title, string desc, bool on)
    {
        var t = new ToggleButton
        {
            IsChecked = on,
            Content = V(2, T(title, null, 13.5, Contrast, FontWeight.SemiBold),
                           T(desc, null, 12, Secondary, wrap: true)),
        };
        t.Classes.Add("tile");
        return t;
    }

    // ------------------------------------------------------------ images

    public static Border Rounded(Control child, double size, double radius, IBrush? bg = null) => new()
    {
        Width = size,
        Height = size,
        CornerRadius = new CornerRadius(radius),
        ClipToBounds = true,
        Background = bg ?? ButtonBg,
        Child = child,
    };

    public static Image PixelImage(Bitmap? bmp, double size)
    {
        var img = new Image { Source = bmp, Width = size, Height = size, Stretch = Stretch.UniformToFill };
        RenderOptions.SetBitmapInterpolationMode(img, BitmapInterpolationMode.None);
        return img;
    }

    // ------------------------------------------------------------ navigation tabs

    /// <summary>Modrinth's pill tab bar. Returns the bar and a way to move the highlight.</summary>
    public static (Border bar, Action<string> select) NavTabs(IEnumerable<(string key, string label, string? icon)> tabs,
                                                              string active, Action<string> onPick)
    {
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 2 };
        var buttons = new Dictionary<string, Button>();
        void Select(string key)
        {
            foreach (var (k, b) in buttons) b.Classes.Set("active", k == key);
        }
        foreach (var (key, label, icon) in tabs)
        {
            var b = Btn(label, null, icon, "tab");
            var k = key;
            b.Click += (_, _) =>
            {
                Select(k);
                onPick(k);
            };
            buttons[key] = b;
            row.Children.Add(b);
        }
        Select(active);
        var bar = new Border
        {
            Background = Raised,
            BorderBrush = Divider,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(4),
            HorizontalAlignment = HorizontalAlignment.Left,
            BoxShadow = BoxShadows.Parse("0 2 4 0 #40000000"),
            Child = row,
        };
        return (bar, Select);
    }

    /// <summary>Invoke on Enter in a single-line box.</summary>
    public static T OnEnter<T>(this T box, Action a) where T : Control
    {
        box.AddHandler(InputElement.KeyDownEvent, (_, e) =>
        {
            if (e.Key == Key.Enter)
            {
                e.Handled = true;
                a();
            }
        }, Avalonia.Interactivity.RoutingStrategies.Tunnel);
        return box;
    }

    public static T Also<T>(this T c, Action<T> f)
    {
        f(c);
        return c;
    }

    public static string Plural(long n, string one, string? many = null) =>
        n + " " + (n == 1 ? one : many ?? one + "s");
}
