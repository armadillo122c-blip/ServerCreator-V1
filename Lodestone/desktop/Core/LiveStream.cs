using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Lodestone.Desktop.Core;

/// <summary>
/// /api/stream is server-sent events: "line" for each console line and
/// "status" every 1.5 s. Events are raised on a background thread; the shell
/// marshals them to the UI thread.
///
/// Console sequence numbers belong to one server's runtime, so a stream opened
/// for one server is meaningless for the next. The shell restarts it on every
/// switch rather than reusing the cursor (the old page reused it, which is why
/// it could miss the new server's lines).
/// </summary>
public sealed class LiveStream
{
    static readonly HttpClient Http = new() { Timeout = Timeout.InfiniteTimeSpan };

    public event Action<long, string>? Line;
    public event Action<JsonNode>? Status;
    public event Action<bool>? Connected;

    CancellationTokenSource? _cts;
    long _last;

    public long LastSeq => Interlocked.Read(ref _last);

    public void Start(long since)
    {
        Stop();
        Interlocked.Exchange(ref _last, since);
        var cts = new CancellationTokenSource();
        _cts = cts;
        _ = Task.Run(() => Run(cts.Token));
    }

    public void Stop()
    {
        var c = _cts;
        _cts = null;
        if (c == null) return;
        try { c.Cancel(); } catch (ObjectDisposedException) { }
    }

    async Task Run(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Get,
                    Api.Url("/api/stream?since=" + LastSeq));
                using var resp = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);
                resp.EnsureSuccessStatusCode();
                await using var s = await resp.Content.ReadAsStreamAsync(ct);
                using var rd = new StreamReader(s, Encoding.UTF8);
                Connected?.Invoke(true);
                string? ev = null;
                var data = new StringBuilder();
                while (!ct.IsCancellationRequested)
                {
                    var line = await rd.ReadLineAsync(ct);
                    if (line == null) break;
                    if (line.Length == 0)
                    {
                        Dispatch(ev, data.ToString());
                        ev = null;
                        data.Clear();
                        continue;
                    }
                    if (line.StartsWith("event:", StringComparison.Ordinal))
                        ev = line[6..].Trim();
                    else if (line.StartsWith("data:", StringComparison.Ordinal))
                        data.Append(line[5..].TrimStart());
                }
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch (Exception)
            {
                // fall through to the reconnect below
            }
            if (ct.IsCancellationRequested) return;
            Connected?.Invoke(false);
            try { await Task.Delay(2500, ct); }
            catch (OperationCanceledException) { return; }
        }
    }

    void Dispatch(string? ev, string data)
    {
        if (ev == null || data.Length == 0) return;
        JsonNode? j;
        try { j = JsonNode.Parse(data); }
        catch (JsonException) { return; }
        if (j == null) return;
        if (ev == "line")
        {
            var seq = j.L("seq");
            Interlocked.Exchange(ref _last, seq);
            Line?.Invoke(seq, j.S("text"));
        }
        else if (ev == "status")
        {
            Status?.Invoke(j);
        }
    }
}
