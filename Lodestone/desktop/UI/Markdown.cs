using System.Text;
using System.Text.RegularExpressions;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Layout;
using Avalonia.Media;

namespace Lodestone.Desktop.UI;

/// <summary>
/// Enough Markdown for the assistant's answers: paragraphs, headings, lists,
/// quotes, fenced code with a copy button, inline code, bold and italics.
/// File references - `src/Main.java:42` in backticks - become links that open
/// the file in the code editor at that line.
/// </summary>
public static class Markdown
{
    static readonly Regex FileRef = new(@"^(?<p>[\w.\-/\\]+\.(?:java|kt|kts|sk|yml|yaml|json|toml|properties|gradle|xml|md|txt|mcmeta|cfg|conf|js|py))(?::(?<l>\d+))?$",
        RegexOptions.Compiled);
    static readonly Regex Inline = new(@"(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)]+\))", RegexOptions.Compiled);

    public static Control Render(string md, Action<string, int>? openFile = null, Func<string, Task>? copy = null)
    {
        var root = Ui.V(10);
        var lines = (md ?? "").Replace("\r\n", "\n").Split('\n');
        var para = new List<string>();

        void Flush()
        {
            if (para.Count == 0) return;
            root.Children.Add(Text(string.Join(" ", para.Select(p => p.Trim())), openFile));
            para.Clear();
        }

        for (var i = 0; i < lines.Length; i++)
        {
            var line = lines[i];
            var t = line.Trim();
            if (t.StartsWith("```", StringComparison.Ordinal))
            {
                Flush();
                var lang = t[3..].Trim();
                var code = new StringBuilder();
                i++;
                while (i < lines.Length && !lines[i].Trim().StartsWith("```", StringComparison.Ordinal))
                {
                    code.Append(lines[i]).Append('\n');
                    i++;
                }
                root.Children.Add(CodeBlock(code.ToString().TrimEnd('\n'), lang, copy));
                continue;
            }
            if (t.Length == 0)
            {
                Flush();
                continue;
            }
            var h = Regex.Match(t, @"^(#{1,4})\s+(.*)$");
            if (h.Success)
            {
                Flush();
                var size = h.Groups[1].Length switch { 1 => 18.0, 2 => 16.0, _ => 14.5 };
                var tb = Text(h.Groups[2].Value, openFile);
                tb.FontSize = size;
                tb.FontWeight = FontWeight.Bold;
                tb.Foreground = Ui.Contrast;
                root.Children.Add(tb);
                continue;
            }
            var li = Regex.Match(line, @"^(\s*)([-*+]|\d+[.)])\s+(.*)$");
            if (li.Success)
            {
                Flush();
                var depth = li.Groups[1].Value.Replace("\t", "    ").Length / 2;
                var marker = char.IsDigit(li.Groups[2].Value[0]) ? li.Groups[2].Value : "•";
                var g = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*"), Margin = new Thickness(depth * 16, 0, 0, 0) };
                var m = Ui.T(marker, null, 14, Ui.Secondary);
                m.Margin = new Thickness(2, 0, 10, 0);
                m.MinWidth = 12;
                g.Children.Add(m);
                var body = Text(li.Groups[3].Value, openFile);
                Grid.SetColumn(body, 1);
                g.Children.Add(body);
                root.Children.Add(g);
                continue;
            }
            if (t.StartsWith('>'))
            {
                Flush();
                root.Children.Add(new Border
                {
                    BorderBrush = Ui.Ring,
                    BorderThickness = new Thickness(3, 0, 0, 0),
                    Padding = new Thickness(12, 2),
                    Child = Text(t.TrimStart('>', ' '), openFile).Also(x => x.Foreground = Ui.Secondary),
                });
                continue;
            }
            if (t.StartsWith('|'))
            {
                Flush();
                var table = new StringBuilder();
                while (i < lines.Length && lines[i].Trim().StartsWith('|'))
                {
                    if (!Regex.IsMatch(lines[i], @"^\s*\|[\s:|-]+\|\s*$")) table.Append(lines[i].Trim()).Append('\n');
                    i++;
                }
                i--;
                root.Children.Add(CodeBlock(table.ToString().TrimEnd('\n'), "", null));
                continue;
            }
            para.Add(line);
        }
        Flush();
        return root;
    }

    /// <summary>One paragraph with its inline formatting.</summary>
    public static SelectableTextBlock Text(string s, Action<string, int>? openFile)
    {
        var tb = new SelectableTextBlock
        {
            TextWrapping = TextWrapping.Wrap,
            FontSize = 14,
            LineHeight = 21,
            Foreground = Ui.Text,
            SelectionBrush = Ui.Hex("#661BD96A"),
        };
        var inl = tb.Inlines = new InlineCollection();
        var pos = 0;
        foreach (Match m in Inline.Matches(s))
        {
            if (m.Index > pos) inl.Add(new Run(s[pos..m.Index]));
            var v = m.Value;
            if (v.StartsWith('`'))
            {
                var code = v[1..^1];
                var fr = FileRef.Match(code);
                if (fr.Success && openFile != null && (code.Contains('/') || code.Contains('\\') || fr.Groups["l"].Success))
                {
                    var path = fr.Groups["p"].Value;
                    var ln = fr.Groups["l"].Success ? int.Parse(fr.Groups["l"].Value) : 0;
                    var link = new Button { Content = Ui.H(4, Icons.Make("file-text", 12, Ui.Brand), Ui.T(code, null, 12.5, Ui.Brand)) };
                    link.Classes.Add("ghost");
                    link.Classes.Add("xs");
                    link.Padding = new Thickness(5, 0);
                    link.MinHeight = 20;
                    ToolTip.SetTip(link, "Open in the code editor");
                    link.Click += (_, _) => openFile(path, ln);
                    inl.Add(new InlineUIContainer { Child = link, BaselineAlignment = BaselineAlignment.Center });
                }
                else
                {
                    inl.Add(new Run(code)
                    {
                        FontFamily = Ui.Mono,
                        FontSize = 13,
                        Foreground = Ui.Hex("#E6C07B"),
                        Background = Ui.Hex("#1F2329"),
                    });
                }
            }
            else if (v.StartsWith("**", StringComparison.Ordinal) || v.StartsWith("__", StringComparison.Ordinal))
                inl.Add(new Run(v[2..^2]) { FontWeight = FontWeight.Bold, Foreground = Ui.Contrast });
            else if (v.StartsWith('['))
            {
                var lm = Regex.Match(v, @"^\[([^\]]+)\]\(([^)]+)\)$");
                inl.Add(new Run(lm.Groups[1].Value) { Foreground = Ui.Blue, TextDecorations = TextDecorations.Underline });
                inl.Add(new Run(" (" + lm.Groups[2].Value + ")") { Foreground = Ui.Secondary, FontSize = 12 });
            }
            else
                inl.Add(new Run(v[1..^1]) { FontStyle = FontStyle.Italic });
            pos = m.Index + m.Length;
        }
        if (pos < s.Length) inl.Add(new Run(s[pos..]));
        return tb;
    }

    public static Control CodeBlock(string code, string lang, Func<string, Task>? copy)
    {
        var text = Ui.Selectable(code, true, 12.5, Ui.Hex("#DCE2E8"));
        text.TextWrapping = TextWrapping.NoWrap;
        var scroll = new ScrollViewer
        {
            Content = text,
            HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto,
            VerticalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled,
            Padding = new Thickness(14, 10, 14, 12),
        };
        var head = Ui.Split(Ui.T(lang.Length > 0 ? lang : "text", null, 11.5, Ui.Secondary, FontWeight.SemiBold),
            copy == null ? null : Ui.Btn("Copy", () => _ = copy(code), "copy", "ghost xs"));
        head.Margin = new Thickness(14, 6, 6, 0);
        return new Border
        {
            Background = Ui.Terminal,
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(10),
            Child = Ui.V(0, head, scroll),
        };
    }

    /// <summary>A unified diff, coloured: additions green, removals red, hunks blue.</summary>
    public static Control Diff(string diff, double maxHeight = 360)
    {
        var tb = new SelectableTextBlock
        {
            FontFamily = Ui.Mono,
            FontSize = 12.5,
            LineHeight = 18,
            SelectionBrush = Ui.Hex("#661BD96A"),
            TextWrapping = TextWrapping.NoWrap,
        };
        var inl = tb.Inlines = new InlineCollection();
        foreach (var raw in (diff ?? "").Replace("\r\n", "\n").Split('\n'))
        {
            if (raw.StartsWith("+++", StringComparison.Ordinal) || raw.StartsWith("---", StringComparison.Ordinal)) continue;
            IBrush fg = Ui.Text;
            IBrush? bg = null;
            if (raw.StartsWith('+')) { fg = Ui.Hex("#7EE2A8"); bg = Ui.Hex("#1A2E22"); }
            else if (raw.StartsWith('-')) { fg = Ui.Hex("#FF8FA3"); bg = Ui.Hex("#331A20"); }
            else if (raw.StartsWith("@@", StringComparison.Ordinal)) fg = Ui.Blue;
            else fg = Ui.Secondary;
            var run = new Run(raw + "\n") { Foreground = fg };
            if (bg != null) run.Background = bg;
            inl.Add(run);
        }
        return new Border
        {
            Background = Ui.Terminal,
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(10),
            Child = new ScrollViewer
            {
                Content = tb,
                MaxHeight = maxHeight,
                HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto,
                Padding = new Thickness(12, 8),
            },
        };
    }
}
