using System.Globalization;
using System.Text.Json.Nodes;

namespace Lodestone.Desktop.Core;

/// <summary>
/// The engine answers in loose JSON - the same objects the old page read with
/// JavaScript truthiness. These read a field the way that page did, so a
/// missing key, a null and a wrong type all come back as a sensible default
/// instead of an exception.
/// </summary>
public static class J
{
    public static JsonNode? N(this JsonNode? n, string key)
    {
        if (n is JsonObject o && o.TryGetPropertyValue(key, out var v))
            return v;
        return null;
    }

    public static string S(this JsonNode? n, string key, string def = "")
    {
        // Like the old page's `x || def`: an empty string falls back too.
        var s = Str(n.N(key), def);
        return s.Length == 0 ? def : s;
    }

    public static string Str(JsonNode? v, string def = "")
    {
        if (v == null) return def;
        if (v is JsonValue jv)
        {
            if (jv.TryGetValue(out string? s)) return s ?? def;
            if (jv.TryGetValue(out bool b)) return b ? "true" : "false";
            if (jv.TryGetValue(out long l)) return l.ToString(CultureInfo.InvariantCulture);
            if (jv.TryGetValue(out double d)) return d.ToString(CultureInfo.InvariantCulture);
            return jv.ToJsonString();
        }
        return v.ToJsonString();
    }

    public static long L(this JsonNode? n, string key, long def = 0)
    {
        var v = n.N(key);
        if (v is JsonValue jv)
        {
            if (jv.TryGetValue(out long l)) return l;
            if (jv.TryGetValue(out double d)) return (long)d;
            if (jv.TryGetValue(out string? s) && long.TryParse(s, out var p)) return p;
        }
        return def;
    }

    public static int I(this JsonNode? n, string key, int def = 0) => (int)n.L(key, def);

    public static double D(this JsonNode? n, string key, double def = 0)
    {
        var v = n.N(key);
        if (v is JsonValue jv)
        {
            if (jv.TryGetValue(out double d)) return d;
            if (jv.TryGetValue(out long l)) return l;
            if (jv.TryGetValue(out string? s) &&
                double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var p)) return p;
        }
        return def;
    }

    /// <summary>JavaScript truthiness: false, 0, "" and null are false.</summary>
    public static bool B(this JsonNode? n, string key) => Truthy(n.N(key));

    public static bool Truthy(JsonNode? v)
    {
        if (v == null) return false;
        if (v is JsonValue jv)
        {
            if (jv.TryGetValue(out bool b)) return b;
            if (jv.TryGetValue(out long l)) return l != 0;
            if (jv.TryGetValue(out double d)) return d != 0;
            if (jv.TryGetValue(out string? s)) return !string.IsNullOrEmpty(s);
        }
        return true;
    }

    /// <summary>True only when the key is present and literally false.</summary>
    public static bool IsFalse(this JsonNode? n, string key) =>
        n.N(key) is JsonValue jv && jv.TryGetValue(out bool b) && !b;

    public static bool Has(this JsonNode? n, string key) => n.N(key) != null;

    public static JsonArray A(this JsonNode? n, string key) => n.N(key) as JsonArray ?? new JsonArray();

    public static IEnumerable<JsonNode> Items(this JsonArray a)
    {
        foreach (var x in a)
            if (x != null) yield return x;
    }

    public static List<string> Strings(this JsonNode? n, string key) =>
        n.A(key).Select(x => Str(x)).ToList();

    public static JsonNode Clone(this JsonNode n) => JsonNode.Parse(n.ToJsonString())!;

    /// <summary>Walk a dotted path the way the old page's get() did.</summary>
    public static JsonNode? Path(this JsonNode? n, string path)
    {
        foreach (var k in path.Split('.'))
            n = n.N(k);
        return n;
    }

    public static void SetPath(this JsonObject o, string path, JsonNode? value)
    {
        var ks = path.Split('.');
        var cur = o;
        for (var i = 0; i < ks.Length - 1; i++)
        {
            if (cur[ks[i]] is not JsonObject next)
            {
                next = new JsonObject();
                cur[ks[i]] = next;
            }
            cur = next;
        }
        cur[ks[^1]] = value;
    }
}
