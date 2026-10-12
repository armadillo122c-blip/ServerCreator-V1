using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>The token-authenticated /api/v1 surface for scripts.</summary>
public sealed class ApiPage : Page
{
    public ApiPage(MainWindow s) : base(s) { }

    public override string Key => "api";
    public override string Title => "API";
    public override string Icon => "code";
    public override bool ServerScoped => false;
    public override bool Advanced => true;

    static readonly (string verb, string path, string what, string scope)[] Endpoints =
    {
        ("GET", "/api/v1/health", "Panel version and uptime", "read"),
        ("GET", "/api/v1/servers", "Every server on the shelf", "read"),
        ("GET", "/api/v1/status", "Active server status, players, uptime", "read"),
        ("GET", "/api/v1/console?since=N", "Console lines after sequence N", "read"),
        ("GET", "/api/v1/players", "Who is online right now", "read"),
        ("POST", "/api/v1/command", "Run one command - {command}", "control"),
        ("POST", "/api/v1/start", "Start the active server", "control"),
        ("POST", "/api/v1/stop", "Stop it cleanly", "control"),
        ("POST", "/api/v1/restart", "Stop then start", "control"),
        ("POST", "/api/v1/player", "Moderate - {player, action, reason}", "control"),
        ("GET", "/api/v1/files?path=", "List a folder", "read"),
        ("GET", "/api/v1/file?path=", "Read a text file", "read"),
        ("PUT", "/api/v1/file", "Write a file - {path, content}", "files"),
        ("GET", "/api/v1/properties", "server.properties as JSON", "read"),
        ("PATCH", "/api/v1/properties", "Change keys - {values:{k:v}}", "files"),
        ("GET", "/api/v1/backups", "List backups", "read"),
        ("POST", "/api/v1/backup", "Start one - {label, what}", "control"),
        ("POST", "/api/v1/webhook", "Fire a custom Discord event - {title, description, event}", "control"),
        ("POST", "/api/v1/assistant", "Run the assistant - {instruction}", "control"),
        ("GET", "/api/v1/content", "Installed mods or plugins, with problems", "read"),
        ("POST", "/api/v1/content/install", "Install - {items:[{source, project_id, version_id?}]}", "files"),
        ("POST", "/api/v1/content/update", "Update - {files?} (all when omitted)", "files"),
        ("POST", "/api/v1/servers/create", "Create a server - same body as the wizard; returns a job", "admin"),
        ("GET", "/api/v1/jobs?id=", "Progress of a create job", "read"),
    };

    ToggleButton _on = null!;
    StackPanel _secret = null!, _tokens = null!, _eps = null!;
    bool _loading;

    protected override Control Build()
    {
        _on = Ui.Switch("Enabled", false, async v =>
        {
            if (_loading) return;
            await Ops.Act("/api/tokens/enable", new { enabled = v }, v ? "API on." : "API off.");
        });
        _secret = Ui.V(0);
        _tokens = Ui.V(8);
        _eps = Ui.V(0);
        var head = Ui.Split(Ui.V(4, Ui.H1("REST API"),
                Ui.Hint("Send Authorization: Bearer <token>. Tokens are shown once and stored as a SHA-256 hash - if you lose one, make " +
                        "another. Scopes: read status and files, control start/stop/commands, files writes and uploads, admin everything " +
                        "including server records.")),
            Ui.H(14, _on, Ui.Btn("New token", NewToken, "key", "brand")));
        var tokens = Ui.Card("Tokens", null, null, Ui.V(12, _secret, _tokens));
        var eps = Ui.Card("Endpoints", "Base: " + Api.Base + "/api/v1", null, _eps);
        return Ui.Scroll(Ui.V(20, head, tokens, eps), new Thickness(28, 24, 28, 28));
    }

    public override Task OnShow() => Load();

    async Task Load()
    {
        var j = await Ops.Get("/api/tokens");
        if (j == null) return;
        _loading = true;
        _on.IsChecked = j.B("enabled");
        _loading = false;
        _tokens.Children.Clear();
        var items = j.A("items").Items().ToList();
        if (items.Count == 0)
            _tokens.Children.Add(Ui.Empty("No tokens yet. The API stays shut until you make one and switch it on.", "key"));
        foreach (var t in items)
        {
            var id = t.S("id");
            _tokens.Children.Add(Ui.Row(
                Ui.Lead(Ui.Rounded(Icons.Make("key", 18, Ui.Secondary), 38, 10),
                    Ui.TitleSub(t.S("name"), t.S("prefix") + "… · made " + t.S("created") + " · " + t.S("calls", "0") + " calls · last " + t.S("last_used", "never"),
                        Ui.H(4, t.Strings("scopes").Select(sc => (Control)Ui.Tag(sc)).ToArray()))),
                Ui.Btn("Revoke", () => Revoke(id), "trash", "soft-red sm")));
        }

        _eps.Children.Clear();
        foreach (var (verb, path, what, scope) in Endpoints)
        {
            var kind = verb switch { "GET" => "on", "DELETE" => "bad", "PUT" or "PATCH" => "warn", _ => "info" };
            var badge = Ui.Pill(verb, kind, false);
            badge.MinWidth = 64;
            ((TextBlock)badge.Child!).HorizontalAlignment = HorizontalAlignment.Center;
            var g = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,270,*,Auto"), Margin = new Thickness(0, 7) };
            g.Children.Add(badge);
            var p = Ui.MonoText(path, 13, Ui.Contrast);
            p.Margin = new Thickness(12, 0, 16, 0);
            p.VerticalAlignment = VerticalAlignment.Center;
            Grid.SetColumn(p, 1);
            g.Children.Add(p);
            var w = Ui.T(what, null, 13, Ui.Secondary);
            w.VerticalAlignment = VerticalAlignment.Center;
            w.TextTrimming = TextTrimming.CharacterEllipsis;
            Grid.SetColumn(w, 2);
            g.Children.Add(w);
            var sc = Ui.Tag(scope);
            Grid.SetColumn(sc, 3);
            g.Children.Add(sc);
            _eps.Children.Add(g);
            _eps.Children.Add(Ui.Divider1(0));
        }

        var code = new TextBlock { FontFamily = Ui.Mono, FontSize = 12.5, LineHeight = 20, TextWrapping = TextWrapping.Wrap };
        void C(string t) => code.Inlines!.Add(new Run(t) { Foreground = Ui.Secondary });
        void M(string t) => code.Inlines!.Add(new Run(t) { Foreground = Ui.Brand });
        void U(string t) => code.Inlines!.Add(new Run(t) { Foreground = Ui.Blue });
        void X(string t) => code.Inlines!.Add(new Run(t) { Foreground = Ui.Text });
        C("# every call carries the token\n"); M("curl "); U(Api.Base + "/api/v1/status"); X(" \\\n  -H "); U("\"Authorization: Bearer lds_…\"\n\n");
        C("# run a command\n"); M("curl "); X("-X POST "); U(Api.Base + "/api/v1/command"); X(" \\\n  -H "); U("\"Authorization: Bearer lds_…\"");
        X(" \\\n  -H "); U("\"Content-Type: application/json\""); X(" \\\n  -d "); U("'{\"command\":\"say hello from a script\"}'");
        var box = new Border { Child = code, Margin = new Thickness(0, 14, 0, 0) };
        box.Classes.Add("code");
        _eps.Children.Add(box);
    }

    async void NewToken()
    {
        var name = Ui.Input("", "deploy script");
        var scopes = new List<(string key, ToggleButton tile)>();
        var grid = new UniformGrid { Columns = 2 };
        foreach (var (k, t, d) in new[]
                 {
                     ("read", "Read", "Status, console, files, properties"),
                     ("control", "Control", "Start, stop, commands, moderation, backups"),
                     ("files", "Files", "Write files and change properties"),
                     ("admin", "Admin", "Everything, including server records"),
                 })
        {
            var tile = Ui.Tile(t, d, k == "read");
            tile.Margin = new Thickness(0, 0, 8, 8);
            scopes.Add((k, tile));
            grid.Children.Add(tile);
        }
        var body = Ui.V(0, Ui.Field("Name", name), Ui.Label("Scopes"), grid);
        if (await Dialogs.Show("New API token", body, ("Cancel", "", "ghost"), ("Create", "ok", "brand")) != "ok") return;
        var picked = new JsonArray(scopes.Where(s => s.tile.IsChecked == true).Select(s => (JsonNode?)s.key).ToArray());
        var j = await Ops.Act("/api/tokens/create",
            new JsonObject { ["name"] = string.IsNullOrWhiteSpace(name.Text) ? "token" : name.Text, ["scopes"] = picked }, "Token created.");
        if (j == null) return;
        var token = j.S("token");
        _secret.Children.Clear();
        var secret = new Border
        {
            Background = Ui.BrandTint,
            BorderBrush = Ui.Brand,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(12),
            Padding = new Thickness(14, 12),
            Child = Ui.V(8,
                Ui.T("Copy this now - it is not shown again.", null, 13.5, Ui.Brand, FontWeight.SemiBold),
                Ui.Split(Ui.Selectable(token, true, 13, Ui.Contrast), Ui.Btn("Copy", () => _ = S.Copy(token), "copy", "sm"))),
        };
        _secret.Children.Add(secret);
        await Load();
    }

    async void Revoke(string id)
    {
        if (!await Dialogs.Confirm("Revoke this token?", "Anything using it stops working immediately.", "Revoke")) return;
        await Ops.Act("/api/tokens/delete", new { id }, "Revoked.");
        await Load();
    }
}
