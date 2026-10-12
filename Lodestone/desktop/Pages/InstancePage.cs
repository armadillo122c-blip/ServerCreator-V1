using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// The open server's own setup: its name, folder, jar, Java and memory, the
/// SFTP login for a remote one, RCON, and how it looks in the library. It used
/// to be an editor you reached from the server list; now it is a tab inside the
/// instance, next to everything else about that server.
/// </summary>
public sealed class InstancePage : Page
{
    public InstancePage(MainWindow s) : base(s) { }

    public override string Key => "instance";
    public override string Title => "Instance settings";
    public override string Icon => "sliders";
    public override string Group => "Manage";

    JsonObject? _editing;
    StackPanel _edit = null!;
    readonly List<(string? group, string key, Func<JsonNode?> read)> _fields = new();
    TextBlock _sftpMsg = new();
    StackPanel _sftpOut = new();
    List<JsonNode> _profiles = new();
    string _aikar = "";

    protected override Control Build()
    {
        _edit = Ui.V(16);
        return Ui.Scroll(_edit, new Thickness(28, 14, 28, 28));
    }

    public override Task OnShow() => Load();

    public override void OnServerSwitched()
    {
        _editing = null;
        if (Showing) _ = Load();
    }

    string CurrentId => S.Servers.FirstOrDefault(x => x.B("active")).S("id", S.State.N("server").S("id"));

    async Task Load()
    {
        var id = CurrentId;
        if (id.Length == 0) return;
        try
        {
            var p = await Api.Get("/api/profiles");
            _profiles = p.A("items").Items().ToList();
            _aikar = p.S("aikar");
        }
        catch (Exception)
        {
            _profiles = new List<JsonNode>();
        }
        try
        {
            var j = await Api.Get("/api/servers/get?id=" + Api.Q(id));
            _editing = (JsonObject)j.N("server")!.DeepClone();
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
            return;
        }
        RenderEdit();
    }

    JsonNode? Profile(string id) => id.Length == 0 ? null : _profiles.FirstOrDefault(p => p.S("id") == id);

    /// <summary>"block/grass_block" as "Grass Block".</summary>
    static string Pretty(string key)
    {
        var name = key.Contains('/') ? key[(key.IndexOf('/') + 1)..] : key;
        return string.Join(" ", name.Split('_', StringSplitOptions.RemoveEmptyEntries)
            .Select(w => char.ToUpperInvariant(w[0]) + w[1..]));
    }

    TextBox Fld(string? group, string key, string? placeholder = null, bool pw = false, bool mono = false)
    {
        var src = group == null ? _editing : _editing.N(group);
        var t = Ui.Input(src.S(key), placeholder, pw, mono);
        _fields.Add((group, key, () => t.Text ?? ""));
        return t;
    }

    ToggleButton Sw(string group, string key, string label)
    {
        var t = Ui.Switch(label, _editing.N(group).B(key));
        _fields.Add((group, key, () => t.IsChecked == true));
        return t;
    }

    void Collect()
    {
        if (_editing == null) return;
        foreach (var (g, k, read) in _fields)
        {
            if (g == null)
            {
                _editing[k] = read();
                continue;
            }
            if (_editing[g] is not JsonObject o)
            {
                o = new JsonObject();
                _editing[g] = o;
            }
            o[k] = read();
        }
    }

    JsonObject Look()
    {
        if (_editing!["look"] is not JsonObject o)
        {
            o = new JsonObject { ["banner"] = "", ["icon"] = "" };
            _editing["look"] = o;
        }
        return o;
    }

    void RenderEdit()
    {
        var s = _editing!;
        _fields.Clear();
        _edit.Children.Clear();
        _edit.Children.Add(Ui.Split(
            Ui.V(3, Ui.H2("Instance settings"),
                Ui.Hint("Where this server lives, how Java runs it, and how it looks in your library. " +
                        "Game rules like the MOTD and difficulty are in Properties.")),
            Ui.H(8, Ui.Btn("Undo changes", () => _ = Load(), "refresh", "ghost", "Throw away what you changed here"),
                Ui.Btn("Save", () => _ = Save(), "save", "brand"))));

        var kind = Ui.Select(new[] { ("local", "Local - a folder on this machine"), ("sftp", "Remote - reached over SFTP") }, s.S("kind", "local"), v =>
        {
            Collect();
            _editing!["kind"] = v;
            RenderEdit();
        });
        var general = Ui.V(0, Ui.Cols(16, Ui.Field("Name", Fld(null, "name")), Ui.Field("Kind", kind)));
        _edit.Children.Add(Ui.Card("General", null, null, general));

        // ---- look & profile
        var look = s.N("look");
        var bannerPick = ServersPage.Picker(Art.Banners.Select(b => (b.key, b.label)).Prepend(("", "Auto")), look.S("banner"), k =>
        {
            Collect();
            Look()["banner"] = k;
            RenderEdit();
        }, true);
        // The icon: any Minecraft item or block, or the one its software suggests.
        var row = S.Servers.FirstOrDefault(x => x.S("id") == s.S("id"));
        var chosen = look.S("icon");
        var autoKey = row.S("icon_auto");
        var shown = chosen.Length > 0 ? chosen : autoKey;
        var iconPick = Ui.H(14,
            Images.Icon(shown, 64, 16),
            Ui.V(8,
                Ui.T(chosen.Length > 0 ? Pretty(chosen) : "Automatic - " + Pretty(autoKey), null, 14, Ui.Contrast, FontWeight.SemiBold),
                Ui.H(8,
                    Ui.Btn("Choose icon…", async () =>
                    {
                        var pick = await IconPicker.Pick(chosen, autoKey);
                        if (pick == null) return;
                        Collect();
                        Look()["icon"] = pick;
                        RenderEdit();
                    }, "image", "sm"),
                    chosen.Length > 0
                        ? Ui.Btn("Use automatic", () =>
                        {
                            Collect();
                            Look()["icon"] = "";
                            RenderEdit();
                        }, null, "ghost sm")
                        : null)).Also(v => v.VerticalAlignment = Avalonia.Layout.VerticalAlignment.Center));
        var profOpts = new List<(string, string)> { ("", "No profile") };
        profOpts.AddRange(_profiles.Select(p => (p.S("id"), p.S("name"))));
        var profPick = Ui.Select(profOpts, s.S("profile"), v =>
        {
            Collect();
            _editing!["profile"] = v;
            RenderEdit();
        }, 280);
        var cur = Profile(s.S("profile"));
        var profRow = Ui.H(10, profPick);
        if (cur != null && ServersPage.HasSettings(cur) && s.S("kind") == "local")
            profRow.Children.Add(Ui.Btn("Use its Java settings", () =>
            {
                Collect();
                if (_editing!["local"] is not JsonObject lc)
                {
                    lc = new JsonObject();
                    _editing["local"] = lc;
                }
                foreach (var k in new[] { "java", "min_ram", "max_ram", "extra" })
                    if (cur.S(k).Length > 0) lc[k] = cur.S(k);
                RenderEdit();
                Toasts.Show("Filled in from " + cur.S("name") + " - press Save to keep it.");
            }, "check", "sm"));
        _edit.Children.Add(Ui.Card("Look & profile", "How this instance shows in your library, and the profile it belongs to", null, Ui.V(0,
            Ui.Field("Banner", bannerPick),
            Ui.Field("Icon", iconPick, "Any Minecraft item or block. Saved with the rest when you press Save."),
            Ui.Field("Profile", profRow, cur == null ? "Profiles group instances and can hand them shared Java settings - make them on the Instances page, under Profiles."
                                                     : ServersPage.ProfileSummary(cur, _aikar)))));

        if (s.S("kind") == "local")
        {
            var cwd = Fld("local", "cwd", mono: true);
            var jar = Fld("local", "jar", mono: true);
            var cwdRow = WithBrowse(cwd, () => PickFolder(cwd));
            var jarRow = WithBrowse(jar, () => PickJar(jar));
            _edit.Children.Add(Ui.Card("Local server", "Where it lives and how Java runs it", null, Ui.V(0,
                Ui.Cols(16, Ui.Field("Server folder", cwdRow), Ui.Field("Server JAR", jarRow)),
                Ui.Cols(16, Ui.Field("Java command", Fld("local", "java", "java", mono: true)),
                    Ui.Field("Min RAM", Fld("local", "min_ram", "1G", mono: true)),
                    Ui.Field("Max RAM", Fld("local", "max_ram", "4G", mono: true))),
                Ui.Field("Extra JVM flags", Fld("local", "extra", mono: true), "Passed straight through, space separated."))));
        }
        else
        {
            var sftpOut = _sftpOut = Ui.V(8);
            var sftpMsg = _sftpMsg = Ui.Hint("Checks each step in turn and tells you which one fails.");
            _edit.Children.Add(Ui.Card("SFTP", "The box the server lives on", null, Ui.V(0,
                Ui.Cols(16, Ui.Field("Host", Fld("sftp", "host")), Ui.Field("Port", Fld("sftp", "port")), Ui.Field("Username", Fld("sftp", "user"))),
                Ui.Cols(16,
                    Ui.Field("Password", Fld("sftp", "password", pw: true), "Stored in plain text in ~/.lodestone.json."),
                    Ui.Field("Server folder on the host", Fld("sftp", "root", mono: true),
                        "Where server.properties lives. On Minehut that is usually /server, not /.")),
                Ui.H(12, Ui.Btn("Test connection", () => _ = TestSftp(), "plug", "sm"), sftpMsg),
                sftpOut)));

            var mh = s.N("minehut");
            var mhMsg = Ui.Hint("");
            _edit.Children.Add(Ui.Card("Minehut", "Start, stop and send commands - things SFTP cannot do",
                Sw("minehut", "enabled", "Enabled"), Ui.V(0,
                    Ui.Field("Server ID", Fld("minehut", "server_id", "the id in your dashboard URL", mono: true),
                        "The long hex string in dashboard.minehut.com/servers/<this> - the same value as your SFTP username."),
                    Ui.Cols(16,
                        Ui.Field("Auth token", Fld("minehut", "token", mh.B("has_token") ? "saved - type to replace" : "minehut_auth_token", true)),
                        Ui.Field("Session id", Fld("minehut", "session_id", mh.B("has_session") ? "saved - type to replace" : "minehut_session_id", true))),
                    Ui.Hint("Minehut has no official API and no way to log in from a script, so these come from the dashboard itself: " +
                            "open dashboard.minehut.com, press F12, and in Application (Chrome) or Storage (Firefox) → Local Storage copy " +
                            "minehut_auth_token and minehut_session_id. Since 2026 the dashboard logs in with a secure cookie and may not " +
                            "keep them there any more - then leave these empty: Lodestone still shows the server's status from Minehut's " +
                            "public info, and you start and stop it in the dashboard. They expire, so if actions start failing with a 401, " +
                            "grab them again. Stored in ~/.lodestone.json and never shown back to you. Because the API is unofficial, " +
                            "Minehut can change it at any time.")
                        .Also(h => h.Margin = new Thickness(0, 0, 0, 12)),
                    Ui.H(12, Ui.Btn("Test connection", () => _ = TestMinehut(mhMsg), "plug", "sm"), mhMsg))));
        }

        var rconMsg = Ui.Hint("");
        _edit.Children.Add(Ui.Card("RCON", "Lets the console send commands when the panel does not own the process",
            Sw("rcon", "enabled", "Enabled"), Ui.V(0,
                Ui.Cols(16,
                    Ui.Field("Host", Fld("rcon", "host"), s.S("kind") == "sftp" ? "Blank uses the SFTP host." : "Blank uses 127.0.0.1"),
                    Ui.Field("Port", Fld("rcon", "port")),
                    Ui.Field("Password", Fld("rcon", "password", pw: true), "Must match rcon.password in server.properties.")),
                Ui.H(12, Ui.Btn("Test connection", () => _ = TestRcon(rconMsg), "plug", "sm"), rconMsg))));

        _edit.Children.Add(Ui.Card("This instance", "Copy it, or take it out of Lodestone", null, Ui.V(10,
            Ui.Row(Ui.TitleSub("Duplicate", "A second entry with the same settings, pointing at the same folder - change the folder to make it a real copy."),
                Ui.Btn("Duplicate", () => _ = Dup(), "copy", "sm")),
            Ui.Row(Ui.TitleSub("Remove from Lodestone", "Only the entry goes. The server's folder and its world stay exactly where they are."),
                Ui.Btn("Remove", () => _ = Remove(), "trash", "soft-red sm")))));
    }

    static Grid WithBrowse(TextBox box, Action browse)
    {
        var g = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        g.Children.Add(box);
        var b = Ui.Btn("Browse", browse, "folder", "sm");
        b.Margin = new Thickness(8, 0, 0, 0);
        Grid.SetColumn(b, 1);
        g.Children.Add(b);
        return g;
    }

    async void PickFolder(TextBox target)
    {
        var r = await S.StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title = "Select the server folder" });
        var p = r.FirstOrDefault()?.TryGetLocalPath();
        if (p != null) target.Text = p;
    }

    async void PickJar(TextBox target)
    {
        var r = await S.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
        {
            Title = "Select the server JAR",
            FileTypeFilter = new[] { new FilePickerFileType("Server JAR") { Patterns = new[] { "*.jar" } }, FilePickerFileTypes.All },
        });
        var p = r.FirstOrDefault()?.TryGetLocalPath();
        if (p != null) target.Text = p;
    }

    async Task Save()
    {
        Collect();
        if (await Ops.Act("/api/servers/save", _editing, "Saved.") == null) return;
        await S.LoadShelf();
        S.RefreshIcon();
        await Load();
    }

    async Task<bool> SaveQuiet()
    {
        Collect();
        return await Ops.Act("/api/servers/save", _editing, null) != null;
    }

    async Task Dup()
    {
        var j = await Ops.Act("/api/servers/duplicate", new { id = CurrentId }, "Duplicated - the copy is in your library.");
        if (j != null) await S.LoadShelf();
    }

    async Task Remove()
    {
        var name = _editing.S("name", "this server");
        if (!await Dialogs.Confirm("Remove " + name + "?", "The server files are left alone - this only removes the entry here.", "Remove"))
            return;
        if (await Ops.Act("/api/servers/delete", new { id = CurrentId }, "Removed " + name + ".") == null) return;
        await S.LoadShelf();
        S.GoHome();
    }

    public async Task TestSftp()
    {
        var msg = _sftpMsg;
        var output = _sftpOut;
        if (!await SaveQuiet()) return;
        msg.Text = "testing…";
        output.Children.Clear();
        try
        {
            var j = await Api.Post("/api/sftp/test", new { id = _editing!.S("id") });
            msg.Text = j.B("ok") ? "everything checks out" : "stopped at the step marked below";
            foreach (var st in j.A("steps").Items())
                output.Children.Add(Ui.Row(Ui.Lead(Ui.Pill(st.B("ok") ? "ok" : "fail", st.B("ok") ? "on" : "bad"),
                    Ui.TitleSub(st.S("step"), st.S("detail")))));
            var root = j.S("suggest_root");
            if (root.Length > 0)
            {
                var row = Ui.Row(Ui.TitleSub("The server files are in " + root, "Use that as the server folder?"),
                    Ui.Btn("Use it", () => _ = UseRoot(root), "check", "brand sm"));
                row.Classes.Add("active");
                output.Children.Add(row);
            }
            var listing = j.Strings("listing");
            if (listing.Count > 0)
            {
                output.Children.Add(Ui.Hint("What is on the account:"));
                output.Children.Add(Ui.Code(string.Join("\n", listing)));
            }
        }
        catch (Exception e)
        {
            msg.Text = e.Message;
        }
    }

    async Task UseRoot(string root)
    {
        await Ops.Act("/api/sftp/use-root", new { id = _editing!.S("id"), root }, "Server folder set to " + root);
        Collect();
        if (_editing!["sftp"] is JsonObject o) o["root"] = root;
        RenderEdit();
        await TestSftp();
    }

    async Task TestMinehut(TextBlock msg)
    {
        if (!await SaveQuiet()) return;
        msg.Text = "asking Minehut…";
        try
        {
            var j = await Api.Post("/api/minehut/test", new { id = _editing!.S("id") });
            msg.Text = j.B("ok") ? j.S("out") : j.S("error");
        }
        catch (Exception e)
        {
            msg.Text = e.Message;
        }
    }

    async Task TestRcon(TextBlock msg)
    {
        if (!await SaveQuiet()) return;
        msg.Text = "testing…";
        try
        {
            var j = await Api.Post("/api/rcon/test", new { id = _editing!.S("id") });
            msg.Text = j.B("ok") ? "connected - " + j.S("out", "no reply") : j.S("error");
        }
        catch (Exception e)
        {
            msg.Text = e.Message;
        }
    }
}
