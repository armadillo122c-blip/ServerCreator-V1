using System.Text;
using System.Text.RegularExpressions;
using Avalonia.Media;

namespace Lodestone.Desktop.UI;

/// <summary>A coloured stretch of a shown console line.</summary>
public readonly record struct LogSeg(int Start, int End, IBrush Brush, bool Bold);

/// <summary>One console line as it is shown: its text, its colours, and when it arrived (for the fade-in).</summary>
public sealed class LogShown
{
    public string Text = "";
    public readonly List<LogSeg> Segs = new();
    public long Born;
}

/// <summary>
/// Turns a raw server line into what the console shows, in one of three modes:
/// <list type="bullet">
/// <item><b>simple</b> - only what a person cares about while playing: chat, joins and leaves, deaths,
/// advancements, commands players ran, the server starting and stopping, Lodestone's own messages and
/// errors (one line each, no stack traces). A short time, no log header.</item>
/// <item><b>advanced</b> - every line, cleaned up: colour codes become colours, the header shrinks to the time
/// and level, plugin tags, names, addresses and stack traces are coloured.</item>
/// <item><b>raw</b> - exactly what the server printed, escape codes included (ESC shown as ␛).</item>
/// </list>
/// Colour codes are read in every form servers use: ANSI SGR (16 colours, 256 and 24-bit, bold), with or
/// without the ESC that some paths drop; § codes; §#rrggbb and Bukkit's §x§r§r§g§g§b§b.
/// </summary>
public static class LogView
{
    // ---------------------------------------------------------------- palette

    static IBrush H(string hex) => new SolidColorBrush(Color.Parse(hex));

    public static readonly IBrush Time = H("#56606E");
    public static readonly IBrush Plain = H("#C9D1DB");
    public static readonly IBrush Bright = H("#EEF2F7");
    public static readonly IBrush InfoLvl = H("#5E7BD6");
    public static readonly IBrush WarnLvl = H("#FFB547");
    public static readonly IBrush WarnText = H("#FFD9A0");
    public static readonly IBrush ErrLvl = H("#FF5C7A");
    public static readonly IBrush ErrText = H("#FFA3B5");
    public static readonly IBrush Logger = H("#A78BFA");
    public static readonly IBrush Join = H("#4ADE80");
    public static readonly IBrush Leave = H("#FB923C");
    public static readonly IBrush Death = H("#F87171");
    public static readonly IBrush Advance = H("#FACC15");
    public static readonly IBrush Command = H("#C084FC");
    public static readonly IBrush System = H("#1BD96A");
    public static readonly IBrush SystemText = H("#9BE8BA");
    public static readonly IBrush Link = H("#60A5FA");
    public static readonly IBrush Address = H("#FDBA74");
    public static readonly IBrush TraceText = H("#D08A96");
    public static readonly IBrush CodeDim = H("#4A5260");

    static readonly string[] NamePalette = { "#1BD96A", "#4F9CFF", "#C78AFF", "#FFA347", "#5EE6D0", "#FF7EB6", "#FFD65C" };

    public static IBrush NameBrush(string name)
    {
        var h = 0;
        foreach (var c in name.ToLowerInvariant()) h = unchecked(h * 31 + c);
        return H(NamePalette[(int)((uint)h % NamePalette.Length)]);
    }

    /// <summary>Minecraft's 16 colours, the darkest lifted so they read on the terminal.</summary>
    static readonly Dictionary<char, string> McColors = new()
    {
        ['0'] = "#5C6370", ['1'] = "#5C7CFF", ['2'] = "#2FBF4F", ['3'] = "#2CC4C4", ['4'] = "#E0474C",
        ['5'] = "#C35BD6", ['6'] = "#FFAA00", ['7'] = "#B0BAC5", ['8'] = "#7A828C", ['9'] = "#6E7BFF",
        ['a'] = "#55FF55", ['b'] = "#55FFFF", ['c'] = "#FF5555", ['d'] = "#FF6EFF", ['e'] = "#FFFF55",
        ['f'] = "#FFFFFF",
    };

    static readonly string[] AnsiColors =
    {
        "#5C6370", "#E0474C", "#2FBF4F", "#FFAA00", "#5C7CFF", "#C35BD6", "#2CC4C4", "#B0BAC5",
        "#7A828C", "#FF5555", "#55FF55", "#FFFF55", "#6E7BFF", "#FF6EFF", "#55FFFF", "#FFFFFF",
    };

    /// <summary>A server's own colour, lifted if it would vanish into the dark terminal.</summary>
    static IBrush Readable(byte r, byte g, byte b)
    {
        var lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0;
        if (lum < 0.32)
        {
            var k = (0.32 - lum) / 0.32 * 0.55;
            r = (byte)(r + (255 - r) * k);
            g = (byte)(g + (255 - g) * k);
            b = (byte)(b + (255 - b) * k);
        }
        return new SolidColorBrush(Color.FromRgb(r, g, b));
    }

    static IBrush Readable(string hex)
    {
        var c = Color.Parse(hex);
        return Readable(c.R, c.G, c.B);
    }

    // ---------------------------------------------------------------- colour codes

    static readonly Regex AnsiEsc = new(@"\x1B\[[0-9;]*m", RegexOptions.Compiled);
    static readonly Regex AnsiBare = new(@"\[(?:\d{1,3}(?:;\d{1,3})*)?m", RegexOptions.Compiled);
    static readonly Regex McCode = new(@"§x(?:§[0-9a-fA-F]){6}|§#[0-9a-fA-F]{6}|§[0-9a-fk-orA-FK-OR]", RegexOptions.Compiled);
    static readonly Regex OtherEsc = new(@"\x1B\[[0-9;?]*[A-Za-z]|\x1B\][^\x07]*\x07|[\x00-\x08\x0B-\x1F\x7F]", RegexOptions.Compiled);

    /// <summary>The line without its colour codes, and the colours they gave each stretch of it.</summary>
    public static (string clean, List<LogSeg> runs) StripCodes(string raw)
    {
        // Without its ESC an SGR sequence is just "[38;2;r;g;bm" - only read it that way when the line is
        // plainly full of them, so "[5m ago]" in ordinary text is left alone.
        var bare = !raw.Contains('\x1B') && (raw.Contains("[0m") || Regex.IsMatch(raw, @"\[\d{1,3};\d{1,3}(?:;\d{1,3})*m"));
        var ansi = bare ? AnsiBare : AnsiEsc;
        var codes = new List<Match>();
        codes.AddRange(ansi.Matches(raw));
        codes.AddRange(McCode.Matches(raw));
        codes.Sort((a, b) => a.Index.CompareTo(b.Index));
        var sb = new StringBuilder(raw.Length);
        var runs = new List<LogSeg>();
        IBrush? cur = null;
        var bold = false;
        var from = 0;
        var at = 0;
        void Close()
        {
            if ((cur != null || bold) && sb.Length > from) runs.Add(new LogSeg(from, sb.Length, cur ?? Bright, bold));
            from = sb.Length;
        }
        foreach (var m in codes)
        {
            if (m.Index < at) continue;
            sb.Append(OtherEsc.Replace(raw[at..m.Index], ""));
            at = m.Index + m.Length;
            Close();
            var v = m.Value;
            if (v[0] == '§')
            {
                var c = char.ToLowerInvariant(v[1]);
                if (c == 'x' && v.Length == 14) { cur = Readable("#" + string.Concat(v.Where((_, i) => i > 2 && i % 2 == 1))); bold = false; }
                else if (c == '#') { cur = Readable(v[1..]); bold = false; }
                else if (McColors.TryGetValue(c, out var hex)) { cur = Readable(hex); bold = false; }
                else if (c == 'l') bold = true;
                else if (c == 'r') { cur = null; bold = false; }
                continue;
            }
            var body = v[(v[0] == '\x1B' ? 2 : 1)..^1];
            var ps = body.Length == 0 ? new[] { "0" } : body.Split(';');
            for (var i = 0; i < ps.Length; i++)
            {
                if (!int.TryParse(ps[i].Length == 0 ? "0" : ps[i], out var p)) continue;
                if (p == 0) { cur = null; bold = false; }
                else if (p == 1) bold = true;
                else if (p == 22) bold = false;
                else if (p == 39) cur = null;
                else if (p is >= 30 and <= 37) cur = Readable(AnsiColors[p - 30]);
                else if (p is >= 90 and <= 97) cur = Readable(AnsiColors[p - 90 + 8]);
                else if (p == 38 && i + 4 < ps.Length && ps[i + 1] == "2"
                         && byte.TryParse(ps[i + 2], out var r) && byte.TryParse(ps[i + 3], out var g) && byte.TryParse(ps[i + 4], out var b))
                {
                    cur = Readable(r, g, b);
                    i += 4;
                }
                else if (p == 38 && i + 2 < ps.Length && ps[i + 1] == "5" && int.TryParse(ps[i + 2], out var n))
                {
                    cur = n < 16 ? Readable(AnsiColors[n]) : Readable(Ansi256(n));
                    i += 2;
                }
                else if (p is 48) i = ps.Length;     // backgrounds are not drawn
            }
        }
        sb.Append(OtherEsc.Replace(raw[at..], ""));
        Close();
        return (sb.ToString(), runs);
    }

    static string Ansi256(int n)
    {
        if (n >= 232) { var v = 8 + (n - 232) * 10; return $"#{v:X2}{v:X2}{v:X2}"; }
        n -= 16;
        int C(int x) => x == 0 ? 0 : 55 + x * 40;
        return $"#{C(n / 36 % 6):X2}{C(n / 6 % 6):X2}{C(n % 6):X2}";
    }

    // ---------------------------------------------------------------- what a line is

    public enum Kind { System, Alert, Echo, Trace, Error, Warn, Chat, Join, Leave, Death, Advancement, PlayerCommand, Up, Stopping, Starting, Info }

    public sealed class Parsed
    {
        public string Raw = "", Clean = "", Time = "", Level = "", Logger = "", Msg = "";
        public int MsgAt;
        public List<LogSeg> Runs = new();
        public Kind Kind = Kind.Info;
        public string Who = "";
        public int WhoAt = -1;        // where the player's name is in Msg, -1 if not known
    }

    static readonly Regex Header = new(
        @"^\[(?<t>\d{1,2}:\d{2}:\d{2})(?:[.,]\d+)?(?:\s+(?<l>[A-Z]+))?\](?:\s*\[(?<th>[^\]]*?)/(?<l2>[A-Z]+)\])?(?:\s*\[(?<lg>[^\]]{1,60})\])?:?\s*",
        RegexOptions.Compiled);
    static readonly Regex Trace = new(@"^\s*(at |Caused by:|Suppressed:|\.\.\. \d+ more|[a-z][\w$]*(\.[a-z][\w$]*)+\.[A-Z]\w*(Exception|Error)\b)", RegexOptions.Compiled);
    static readonly Regex Tag = new(@"^\[[^\]\s][^\]]{0,40}\]\s*", RegexOptions.Compiled);
    static readonly Regex VChat = new(@"^(?:\[Not Secure\]\s*)?<([A-Za-z0-9_.]{2,17})>\s?", RegexOptions.Compiled);
    static readonly Regex JoinRx = new(@"^([A-Za-z0-9_.]{2,17}) joined the game", RegexOptions.Compiled);
    static readonly Regex LeaveRx = new(@"^([A-Za-z0-9_.]{2,17}) (left the game|lost connection)", RegexOptions.Compiled);
    static readonly Regex IssuedRx = new(@"^([A-Za-z0-9_.]{2,17}) issued server command: (.*)$", RegexOptions.Compiled);
    static readonly Regex AdvRx = new(@"^([A-Za-z0-9_.]{2,17}) has (made the advancement|completed the challenge|reached the goal) ", RegexOptions.Compiled);
    static readonly Regex DeathRx = new(@"^([A-Za-z0-9_.]{2,17}) (was|were|died|drowned|blew up|hit the ground|fell|burned|went up|walked into|starved|suffocated|withered|froze|discovered|experienced|tried to swim|left the confines)\b", RegexOptions.Compiled);
    static readonly Regex UpRx = new(@"Done \(([0-9.,]+)s\)!", RegexOptions.Compiled);
    static readonly Regex StartRx = new(@"^Starting minecraft server version (\S+)", RegexOptions.Compiled);
    static readonly Regex StopRx = new(@"^(Stopping the server|Stopping server)", RegexOptions.Compiled);
    static readonly string[] ChatSeps = { "»", "›", "→", "➤", "▸", "⇒", "|", ":", ">", "-" };
    static readonly Regex NameTok = new(@"^[A-Za-z0-9_]{3,16}$", RegexOptions.Compiled);

    /// <summary>Read a raw line. <paramref name="players"/> are names known to be players, for chat
    /// that a plugin reformatted into "RANK Name » message".</summary>
    public static Parsed Parse(string raw, ICollection<string>? players = null)
    {
        var p = new Parsed { Raw = raw };
        (p.Clean, p.Runs) = StripCodes(raw);
        var t = p.Clean;
        if (t.StartsWith(">>>", StringComparison.Ordinal)) { p.Kind = Kind.System; p.Msg = t[3..].TrimStart(); p.MsgAt = t.Length - p.Msg.Length; return p; }
        if (t.StartsWith("!!!", StringComparison.Ordinal)) { p.Kind = Kind.Alert; p.Msg = t[3..].TrimStart(); p.MsgAt = t.Length - p.Msg.Length; return p; }
        if (t.StartsWith("> ", StringComparison.Ordinal)) { p.Kind = Kind.Echo; p.Msg = t[2..]; p.MsgAt = 2; return p; }
        if (Trace.IsMatch(t)) { p.Kind = Kind.Trace; p.Msg = t; return p; }
        var h = Header.Match(t);
        if (h.Success)
        {
            p.Time = h.Groups["t"].Value;
            p.Level = h.Groups["l"].Success ? h.Groups["l"].Value : h.Groups["l2"].Value;
            p.Logger = h.Groups["lg"].Success ? h.Groups["lg"].Value : "";
            p.MsgAt = h.Length;
        }
        p.Msg = t[p.MsgAt..];
        // a [PluginName] at the front of the message belongs to the logger
        var tag = Tag.Match(p.Msg);
        if (tag.Success && p.Logger.Length == 0 && !VChat.IsMatch(p.Msg))
        {
            p.Logger = tag.Value.Trim().Trim('[', ']');
            p.MsgAt += tag.Length;
            p.Msg = t[p.MsgAt..];
        }
        var msg = p.Msg;
        // Paper prints every line of a stack trace with its own header
        if (Trace.IsMatch(msg)) { p.Kind = Kind.Trace; return p; }
        if (p.Level is "ERROR" or "SEVERE" or "FATAL") { p.Kind = Kind.Error; return p; }
        if (p.Level is "WARN" or "WARNING") { p.Kind = Kind.Warn; return p; }
        Match m;
        if ((m = VChat.Match(msg)).Success) return Who(p, Kind.Chat, m.Groups[1]);
        if ((m = JoinRx.Match(msg)).Success) return Who(p, Kind.Join, m.Groups[1]);
        if ((m = LeaveRx.Match(msg)).Success) return Who(p, Kind.Leave, m.Groups[1]);
        if ((m = IssuedRx.Match(msg)).Success) return Who(p, Kind.PlayerCommand, m.Groups[1]);
        if ((m = AdvRx.Match(msg)).Success) return Who(p, Kind.Advancement, m.Groups[1]);
        if (UpRx.IsMatch(msg)) { p.Kind = Kind.Up; return p; }
        if (StartRx.IsMatch(msg)) { p.Kind = Kind.Starting; return p; }
        if (StopRx.IsMatch(msg)) { p.Kind = Kind.Stopping; return p; }
        if ((m = DeathRx.Match(msg)).Success && players != null && players.Contains(m.Groups[1].Value)) return Who(p, Kind.Death, m.Groups[1]);
        if (p.Logger.Length == 0 && FormattedChat(p, players)) return p;
        return p;
    }

    static Parsed Who(Parsed p, Kind k, Group g)
    {
        p.Kind = k;
        p.Who = g.Value;
        p.WhoAt = g.Index;
        return p;
    }

    /// <summary>"OWNER Armadillo122 » hi": a separator, with a player's name the last word before it.
    /// The arrows count on their own; ':' '>' '|' '-' only when the name is a player we know.</summary>
    static bool FormattedChat(Parsed p, ICollection<string>? players)
    {
        var msg = p.Msg;
        foreach (var sep in ChatSeps)
        {
            var i = msg.IndexOf(sep, StringComparison.Ordinal);
            if (i <= 0 || i > 80) continue;
            var before = msg[..i].TrimEnd();
            var words = before.Split(new[] { ' ', '[', ']', '(', ')', '<', '>', '{', '}' }, StringSplitOptions.RemoveEmptyEntries);
            if (words.Length == 0 || words.Length > 6) return false;
            var name = words[^1];
            if (!NameTok.IsMatch(name)) return false;
            var known = players != null && players.Contains(name);
            var arrow = sep is "»" or "›" or "→" or "➤" or "▸" or "⇒";
            if (!known && !arrow) return false;
            p.Kind = Kind.Chat;
            p.Who = name;
            p.WhoAt = before.LastIndexOf(name, StringComparison.Ordinal);
            return true;
        }
        return false;
    }

    // ---------------------------------------------------------------- rendering

    sealed class B
    {
        public readonly StringBuilder Sb = new();
        public readonly List<LogSeg> Segs = new();
        public int Len => Sb.Length;

        public void Add(string text, IBrush? brush = null, bool bold = false)
        {
            var s = Sb.Length;
            Sb.Append(text);
            if (brush != null || bold) Span(s, Sb.Length, brush ?? Bright, bold);
        }

        public void Span(int s, int e, IBrush brush, bool bold = false)
        {
            if (e > s) Segs.Add(new LogSeg(s, e, brush, bold));
        }

        public LogShown Done() => new LogShown { Text = Sb.ToString() }.Also(x => x.Segs.AddRange(Segs));
    }

    static readonly Regex Addr = new(@"/?\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b", RegexOptions.Compiled);
    static readonly Regex Url = new(@"https?://\S+", RegexOptions.Compiled);
    static readonly Regex Uuid = new(@"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", RegexOptions.Compiled);

    /// <summary>Append the message part of p (clean text from offset <paramref name="from"/>), coloured by
    /// what it is, with the server's own colours over the top.</summary>
    static void Message(B b, Parsed p, int from, IBrush? baseBrush)
    {
        var at = b.Len;
        var text = p.Clean[from..];
        b.Add(text, baseBrush);
        var shift = at - from;
        var msgStart = p.MsgAt;
        if (p.WhoAt >= 0)
        {
            var s = msgStart + p.WhoAt;
            if (s >= from) b.Span(s + shift, s + shift + p.Who.Length, NameBrush(p.Who), true);
        }
        foreach (Match a in Addr.Matches(text)) b.Span(at + a.Index, at + a.Index + a.Length, Address);
        foreach (Match u in Uuid.Matches(text)) b.Span(at + u.Index, at + u.Index + u.Length, Time);
        foreach (Match u in Url.Matches(text)) b.Span(at + u.Index, at + u.Index + u.Length, Link);
        var up = UpRx.Match(text);
        if (up.Success) b.Span(at + up.Index, at + up.Index + up.Length, System, true);
        foreach (var r in p.Runs)
        {
            var s = Math.Max(r.Start, from);
            if (r.End > s) b.Span(s + shift, r.End + shift, r.Brush, r.Bold);
        }
    }

    static string Short(string time) => time.Length >= 5 ? time[..^3] : time;    // 08:48:51 -> 08:48

    /// <summary>What to show for a raw line in a mode, or null to hide it there.</summary>
    public static LogShown? Render(string raw, string mode, ICollection<string>? players = null)
    {
        if (mode == "raw") return RenderRaw(raw);
        var p = Parse(raw, players);
        return mode == "simple" ? RenderSimple(p) : RenderAdvanced(p);
    }

    static LogShown RenderRaw(string raw)
    {
        var b = new B();
        var text = raw.Replace('\x1B', '␛');
        b.Add(text, Plain);
        var h = Header.Match(text);
        if (h.Success) b.Span(0, h.Length, Time);
        foreach (Match m in Regex.Matches(text, @"␛\[[0-9;]*[A-Za-z]|§x(?:§[0-9a-fA-F]){6}|§#[0-9a-fA-F]{6}|§[0-9a-fk-orA-FK-OR]"))
            b.Span(m.Index, m.Index + m.Length, CodeDim);
        return b.Done();
    }

    static LogShown RenderAdvanced(Parsed p)
    {
        var b = new B();
        switch (p.Kind)
        {
            case Kind.System: b.Add("● ", System, true); Message(b, p, p.MsgAt, SystemText); return b.Done();
            case Kind.Alert: b.Add("▲ ", ErrLvl, true); Message(b, p, p.MsgAt, ErrText); return b.Done();
            case Kind.Echo: b.Add("› ", Command, true); Message(b, p, p.MsgAt, Command); return b.Done();
            case Kind.Trace when p.Time.Length == 0: b.Add("        "); Message(b, p, 0, TraceText); return b.Done();
        }
        if (p.Time.Length > 0) b.Add(p.Time + " ", Time);
        if (p.Level.Length > 0)
        {
            var (brush, bold) = p.Level switch
            {
                "INFO" => (InfoLvl, false),
                "WARN" or "WARNING" => (WarnLvl, true),
                "ERROR" or "SEVERE" or "FATAL" => (ErrLvl, true),
                _ => (Time, false),
            };
            b.Add(p.Level.PadRight(5) + " ", brush, bold);
        }
        if (p.Logger.Length > 0) b.Add(p.Logger + "  ", Logger);
        var baseBrush = p.Kind switch
        {
            Kind.Error => ErrText,
            Kind.Warn => WarnText,
            Kind.Chat => Bright,
            Kind.Join => Join,
            Kind.Leave => Leave,
            Kind.Death => Death,
            Kind.Advancement => Advance,
            Kind.PlayerCommand => Command,
            Kind.Trace => TraceText,
            _ => Plain,
        };
        Message(b, p, p.MsgAt, baseBrush);
        return b.Done();
    }

    static LogShown? RenderSimple(Parsed p)
    {
        var b = new B();
        void Stamp() => b.Add((p.Time.Length > 0 ? Short(p.Time) : "     ") + "  ", Time);
        switch (p.Kind)
        {
            case Kind.Chat:
                Stamp();
                Message(b, p, p.MsgAt, Bright);
                return b.Done();
            case Kind.Join:
                Stamp(); b.Add("→ ", Join, true); b.Add(p.Who, NameBrush(p.Who), true); b.Add(" joined", Join);
                return b.Done();
            case Kind.Leave:
                Stamp(); b.Add("← ", Leave, true); b.Add(p.Who, NameBrush(p.Who), true); b.Add(" left", Leave);
                return b.Done();
            case Kind.Death:
                Stamp(); b.Add("☠ ", Death, true); Message(b, p, p.MsgAt, Death);
                return b.Done();
            case Kind.Advancement:
                Stamp(); b.Add("✦ ", Advance, true); Message(b, p, p.MsgAt, Advance);
                return b.Done();
            case Kind.PlayerCommand:
                {
                    Stamp();
                    var cmd = IssuedRx.Match(p.Msg).Groups[2].Value;
                    b.Add(p.Who, NameBrush(p.Who), true); b.Add(" ran ", Time); b.Add(cmd, Command);
                    return b.Done();
                }
            case Kind.Up:
                {
                    Stamp();
                    var m = UpRx.Match(p.Msg);
                    b.Add("✓ Server is up", System, true); b.Add(" in " + m.Groups[1].Value + "s", SystemText);
                    return b.Done();
                }
            case Kind.Starting:
                Stamp(); b.Add("▶ Starting Minecraft " + StartRx.Match(p.Msg).Groups[1].Value, SystemText);
                return b.Done();
            case Kind.Stopping:
                Stamp(); b.Add("■ Stopping the server", Leave);
                return b.Done();
            case Kind.System:
                b.Add("       ● ", System, true); Message(b, p, p.MsgAt, SystemText);
                return b.Done();
            case Kind.Alert:
                b.Add("       ▲ ", ErrLvl, true); Message(b, p, p.MsgAt, ErrText);
                return b.Done();
            case Kind.Echo:
                b.Add("       › ", Command, true); Message(b, p, p.MsgAt, Command);
                return b.Done();
            case Kind.Error:
                Stamp(); b.Add("▲ ", ErrLvl, true);
                if (p.Logger.Length > 0) b.Add(p.Logger + ": ", ErrLvl);
                Message(b, p, p.MsgAt, ErrText);
                return b.Done();
            default:
                return null;
        }
    }
}
