using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

public sealed class BackupsPage : Page
{
    public BackupsPage(MainWindow s) : base(s) { }

    public override string Key => "backups";
    public override string Title => "Backups";
    public override string Icon => "archive";

    TextBox _label = null!;
    ComboBox _what = null!;
    TextBlock _dir = null!, _msg = null!;
    StackPanel _list = null!;
    bool _wasBusy;

    protected override Control Build()
    {
        _label = Ui.Input("", "Label (optional)", width: 200);
        _what = Ui.Select(new[] { ("world", "Worlds only"), ("all", "Everything") }, "world", width: 160);
        _dir = Ui.T("—", null, 12.5, Ui.Secondary);
        _dir.FontFamily = Ui.Mono;
        _msg = Ui.Hint("Backups are zipped and kept on this machine, under your home folder - remote servers get " +
                       "pulled down over SFTP first. Automation can run these on a timer.");
        var head = Ui.Card(Ui.V(14,
            Ui.Split(Ui.V(3, Ui.H2("Back up now"), _dir),
                Ui.H(8, _label, _what, Ui.Btn("Back up now", () => _ = Make(), "archive", "brand"))),
            _msg));
        _list = Ui.V(8);
        var list = Ui.Card("Your backups", null, Ui.IconBtn("refresh", "Refresh", () => _ = Load(), "sm", 32), _list);
        return Ui.Scroll(Ui.V(16, head, list), new Thickness(28, 16, 28, 28));
    }

    public override Task OnShow() => Load();

    public override void OnStatus(JsonNode s)
    {
        if (!Built) return;
        var bk = s.N("backup");
        var busy = bk.B("busy");
        if (busy) _msg.Text = bk.S("msg", "working…") + (bk.I("pct") > 0 ? "  (" + bk.S("pct") + "%)" : "");
        if (_wasBusy && !busy && Showing) _ = Load();
        _wasBusy = busy;
    }

    async Task Load()
    {
        var j = await Ops.Get("/api/backups");
        if (j == null) return;
        _dir.Text = j.S("dir");
        _list.Children.Clear();
        var items = j.A("items");
        if (items.Count == 0)
        {
            _list.Children.Add(Ui.Empty("No backups yet.", "archive"));
            return;
        }
        foreach (var b in items.Items())
        {
            var name = b.S("name");
            _list.Children.Add(Ui.Row(
                Ui.Lead(Ui.Rounded(Icons.Make("archive", 18, Ui.Secondary), 38, 10),
                    Ui.TitleSub(name, b.S("when") + " · " + b.S("size"))),
                Ui.Btn("Download", () => _ = S.SaveDownload("/api/backup/download?name=" + Api.Q(name), name), "download", "sm"),
                Ui.Btn("Restore", () => Restore(name), "restart", "sm"),
                Ui.IconBtn("trash", "Delete", () => Delete(name), "soft-red", 32)));
        }
    }

    async Task Make()
    {
        var j = await Ops.Act("/api/backup/create", new { label = _label.Text ?? "", what = Ui.Val(_what) ?? "world" }, "Backup started.");
        if (j == null) return;
        _label.Text = "";
        await Task.Delay(1500);
        await Load();
    }

    async void Restore(string name)
    {
        if (!await Dialogs.Confirm("Restore " + name + "?",
                "Files in the backup overwrite what is on the server. Stop the server first - the panel will refuse otherwise.",
                "Restore"))
            return;
        await Ops.Act("/api/backup/restore", new { name }, "Restore started.");
    }

    async void Delete(string name)
    {
        if (!await Dialogs.Confirm("Delete " + name + "?", "The zip is removed from this machine.", "Delete")) return;
        await Ops.Act("/api/backup/delete", new { name }, "Deleted.");
        await Load();
    }
}
