using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// The workspace's git, through the git the user has installed (gittools.py in the engine): the branch and
/// where it stands against its remote, the changed files with their diffs, committing some or all of them,
/// branches, pull (fast-forward only) and push. Nothing here force-pushes or rewrites history.
/// </summary>
public sealed partial class CodePage
{
    StackPanel _gitOut = null!;
    readonly HashSet<string> _gitPicked = new();
    string _gitDiffPath = "";

    async Task LoadGit()
    {
        var j = await Ops.Get("/api/dev/git?ws=" + Api.Q(_ws));
        _gitOut.Children.Clear();
        if (j == null) return;
        if (!j.B("git"))
        {
            _gitOut.Children.Add(Ui.Card("Git is not installed", "Install it, then come back to this tab.", null, Ui.V(8,
                Ui.T("On Windows: winget install --id Git.Git -e   (or the installer from git-scm.com)", null, 13, Ui.Text, wrap: true),
                Ui.Btn("git-scm.com/downloads", () => MainWindow.OpenUrl("https://git-scm.com/downloads"), "external", "sm"))));
            return;
        }
        if (!j.B("repo"))
        {
            _gitOut.Children.Add(Ui.Card("Not a git repository", "Git keeps every version of the project, so a change can always be undone.",
                Ui.Btn("Make it a repository", () => _ = GitAct("init", new JsonObject()), "plus", "brand sm"),
                Ui.Hint("Runs git init here (and adds a .gitignore for Gradle or Maven projects).")));
            return;
        }

        // ---- where it stands
        var ahead = j.I("ahead");
        var behind = j.I("behind");
        var branches = j.A("branches").Items().Select(b => b.S("name")).ToList();
        var branchBox = Ui.Select(branches.Select(b => (b, b)), j.S("branch"), width: 220);
        branchBox.SelectionChanged += (_, _) =>
        {
            var to = Ui.Val(branchBox);
            if (to != null && to != j.S("branch")) _ = GitAct("branch", new JsonObject { ["name"] = to });
        };
        var where = j.S("upstream").Length > 0
            ? "tracks " + j.S("upstream") + (ahead > 0 ? " · " + ahead + " to push" : "") + (behind > 0 ? " · " + behind + " to pull" : "") +
              (ahead == 0 && behind == 0 ? " · up to date" : "")
            : "not on a remote yet - Push sets it up as origin/" + j.S("branch");
        _gitOut.Children.Add(Ui.Card("Branch", where,
            Ui.H(8,
                Ui.Btn("Pull", () => _ = GitAct("pull", new JsonObject()), "download", "sm", "Fast-forward only - never makes a merge commit"),
                Ui.Btn("Push", () => _ = GitAct("push", new JsonObject()), "upload", "sm"),
                Ui.Btn("New branch…", async () =>
                {
                    var name = await Dialogs.Prompt("New branch", "Name", "", "Create");
                    if (!string.IsNullOrWhiteSpace(name)) await GitAct("branch", new JsonObject { ["name"] = name.Trim(), ["create"] = true });
                }, "plus", "ghost sm")),
            Ui.H(10, Icons.Make("layers", 16, Ui.Brand), branchBox)));

        // ---- changes
        var files = j.A("files").Items().ToList();
        _gitPicked.IntersectWith(files.Select(f => f.S("path")));
        var list = Ui.V(4);
        var diffBox = Ui.V(6);
        foreach (var f in files)
        {
            var path = f.S("path");
            var pick = new CheckBox { IsChecked = _gitPicked.Contains(path), VerticalAlignment = VerticalAlignment.Center };
            pick.IsCheckedChanged += (_, _) =>
            {
                if (pick.IsChecked == true) _gitPicked.Add(path);
                else _gitPicked.Remove(path);
            };
            var tag = Ui.Tag(f.S("label"), null, f.S("label") switch
            {
                "new" or "added" => Ui.Brand,
                "deleted" => Ui.Red,
                "conflict" => Ui.Red,
                _ => Ui.Orange,
            });
            var open = new Button
            {
                Content = Ui.T(path + (f.S("from").Length > 0 ? "  ← " + f.S("from") : ""), null, 13, Ui.Text).Also(t => t.FontFamily = Ui.Mono),
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Left,
            };
            open.Classes.Add("nav");
            open.Click += (_, _) => _ = GitDiff(path, f.B("staged") && f.S("work").Length == 0, diffBox);
            list.Children.Add(Ui.Lead(Ui.H(8, pick, tag), open, 8));
        }
        if (files.Count == 0) list.Children.Add(Ui.Hint("Nothing changed since the last commit."));
        var msg = Ui.Area("", 3, "What changed, and why");
        var commitRow = Ui.H(8,
            Ui.Btn("Commit selected", () => _ = Commit(msg, true), "check", "sm"),
            Ui.Btn("Commit all", () => _ = Commit(msg, false), "check", "brand sm"),
            Ui.Btn("Let the assistant write it", () => _ = S.AskAssistant(_ws,
                "Look at the git status and diff of this workspace, write a clear commit message for the changes, and commit them.", false),
                "sparkles", "ghost sm"));
        _gitOut.Children.Add(Ui.Card("Changes", files.Count + " file" + (files.Count == 1 ? "" : "s") + " · click one to see its diff", null,
            Ui.V(10, list, files.Count > 0 ? Ui.V(8, msg, commitRow) : null, diffBox)));
        if (_gitDiffPath.Length > 0 && files.Any(f => f.S("path") == _gitDiffPath)) _ = GitDiff(_gitDiffPath, false, diffBox);

        // ---- history
        var log = Ui.V(2);
        foreach (var c in j.A("log").Items())
            log.Children.Add(Ui.Lead(Ui.MonoText(c.S("short"), 12.5, Ui.Brand),
                Ui.Split(Ui.T(c.S("subject"), null, 13, Ui.Text).Also(t => t.TextTrimming = TextTrimming.CharacterEllipsis),
                    Ui.T(c.S("author") + " · " + (c.S("date").Length >= 10 ? c.S("date")[..10] : c.S("date")), null, 12, Ui.Secondary)), 10));
        if (log.Children.Count == 0) log.Children.Add(Ui.Hint("No commits yet."));
        _gitOut.Children.Add(Ui.Card("History", null, null, log));
    }

    async Task GitDiff(string path, bool staged, StackPanel box)
    {
        _gitDiffPath = path;
        var j = await Ops.Get("/api/dev/git/diff?ws=" + Api.Q(_ws) + "&path=" + Api.Q(path) + (staged ? "&staged=1" : ""));
        box.Children.Clear();
        if (j == null) return;
        var d = j.S("diff");
        box.Children.Add(Ui.Split(Ui.T(path, null, 13, Ui.Contrast, FontWeight.SemiBold),
            Ui.Btn("Open", () => _ = OpenAt(_ws, path, 0), "file-text", "ghost xs")));
        box.Children.Add(d.Length > 0 ? Markdown.Diff(d, 420) : Ui.Hint("No differences to show (a binary file, or only staged)."));
    }

    async Task Commit(TextBox msg, bool selected)
    {
        var message = (msg.Text ?? "").Trim();
        if (message.Length == 0)
        {
            Toasts.Bad("Write what changed first.");
            return;
        }
        if (selected && _gitPicked.Count == 0)
        {
            Toasts.Bad("Tick the files to commit, or use Commit all.");
            return;
        }
        var body = new JsonObject { ["message"] = message };
        if (selected) body["files"] = new JsonArray(_gitPicked.Select(p => (JsonNode)JsonValue.Create(p)!).ToArray());
        if (await GitAct("commit", body)) _gitPicked.Clear();
    }

    async Task<bool> GitAct(string action, JsonObject body)
    {
        body["ws"] = _ws;
        var res = await Ops.Act("/api/dev/git/" + action, body, null);
        if (res != null)
        {
            var say = res.S("out").Length > 0 ? res.S("out") : res.S("msg");
            if (say.Length > 0) Toasts.Show(say.Length > 200 ? say[..200] + "…" : say, "on");
        }
        await LoadGit();
        return res != null;
    }
}
