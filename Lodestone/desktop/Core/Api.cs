using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Lodestone.Desktop.Core;

public sealed class ApiException : Exception
{
    public int Status { get; }
    public JsonNode? Body { get; }

    public ApiException(string message, int status = 0, JsonNode? body = null) : base(message)
    {
        Status = status;
        Body = body;
    }
}

/// <summary>
/// The engine's UI routes (/api/...). They take no token - they are bound to
/// 127.0.0.1 unless the user turned LAN access on - so this is a thin JSON
/// client with the same error rules the browser page used: a non-2xx reply or
/// {"ok": false} carrying an "error" becomes an exception.
/// </summary>
public static class Api
{
    public static string Base { get; set; } = "http://127.0.0.1:8744";

    // Long enough for the slow routes: the assistant, auto fix, installing Via.
    static readonly HttpClient Http = new() { Timeout = TimeSpan.FromMinutes(15) };

    public static string Url(string path) => Base + path;

    public static string Q(string? s) => Uri.EscapeDataString(s ?? "");

    public static Task<JsonNode> Get(string path) =>
        Send(new HttpRequestMessage(HttpMethod.Get, Url(path)));

    public static Task<JsonNode> Post(string path, object? body = null)
    {
        var json = body switch
        {
            null => "{}",
            JsonNode n => n.ToJsonString(),
            string s => s,
            _ => JsonSerializer.Serialize(body),
        };
        var req = new HttpRequestMessage(HttpMethod.Post, Url(path))
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json"),
        };
        return Send(req);
    }

    /// <summary>Raw-body uploads: files and the server icon.</summary>
    public static Task<JsonNode> PostRaw(string path, Stream data)
    {
        var content = new StreamContent(data);
        content.Headers.ContentType = new MediaTypeHeaderValue("application/octet-stream");
        return Send(new HttpRequestMessage(HttpMethod.Post, Url(path)) { Content = content });
    }

    static async Task<JsonNode> Send(HttpRequestMessage req)
    {
        HttpResponseMessage resp;
        try
        {
            resp = await Http.SendAsync(req).ConfigureAwait(false);
        }
        catch (HttpRequestException e)
        {
            throw new ApiException("Could not reach the Lodestone engine: " + e.Message);
        }
        catch (TaskCanceledException)
        {
            throw new ApiException("The Lodestone engine took too long to answer.");
        }
        using (resp)
        {
            var text = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
            JsonNode j;
            try
            {
                j = JsonNode.Parse(string.IsNullOrWhiteSpace(text) ? "{}" : text) ?? new JsonObject();
            }
            catch (JsonException)
            {
                j = new JsonObject { ["ok"] = false, ["error"] = "Bad reply from the panel." };
            }
            var err = j.S("error");
            if (!resp.IsSuccessStatusCode && err.Length > 0)
                throw new ApiException(err, (int)resp.StatusCode, j);
            if (j.IsFalse("ok") && err.Length > 0)
                throw new ApiException(err, (int)resp.StatusCode, j);
            if (!resp.IsSuccessStatusCode && resp.StatusCode != HttpStatusCode.Conflict)
                throw new ApiException("The engine answered " + (int)resp.StatusCode + ".", (int)resp.StatusCode, j);
            return j;
        }
    }

    /// <summary>Stream a download (a file, a zipped folder, a backup) to disk.</summary>
    public static async Task DownloadTo(string path, Stream dest, IProgress<long>? progress = null)
    {
        using var resp = await Http.GetAsync(Url(path), HttpCompletionOption.ResponseHeadersRead)
            .ConfigureAwait(false);
        if (!resp.IsSuccessStatusCode)
        {
            var text = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
            string msg;
            try { msg = JsonNode.Parse(text).S("error", "Download failed."); }
            catch (JsonException) { msg = "Download failed (" + (int)resp.StatusCode + ")."; }
            throw new ApiException(msg, (int)resp.StatusCode);
        }
        await using var src = await resp.Content.ReadAsStreamAsync().ConfigureAwait(false);
        var buf = new byte[81920];
        long total = 0;
        int n;
        while ((n = await src.ReadAsync(buf).ConfigureAwait(false)) > 0)
        {
            await dest.WriteAsync(buf.AsMemory(0, n)).ConfigureAwait(false);
            total += n;
            progress?.Report(total);
        }
    }

    /// <summary>Raw GET for binary replies such as the server icon.</summary>
    public static async Task<byte[]?> Bytes(string path)
    {
        try
        {
            using var resp = await Http.GetAsync(Url(path)).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode) return null;
            return await resp.Content.ReadAsByteArrayAsync().ConfigureAwait(false);
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// <summary>Anything on the internet, for player heads. Never the engine.</summary>
    public static async Task<byte[]?> External(string url)
    {
        try
        {
            using var resp = await Http.GetAsync(url).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode) return null;
            return await resp.Content.ReadAsByteArrayAsync().ConfigureAwait(false);
        }
        catch (Exception)
        {
            return null;
        }
    }
}
