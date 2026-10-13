using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// The assistant's skills: packaged instructions (a folder with a SKILL.md) it loads when a request matches,
/// or that you start by typing /name. Listed by where they come from, switched on and off, written and edited
/// here, imported from a folder or a .zip/.skill file. See skills.py in the engine.
/// </summary>
public sealed partial class AgentPage
{
    StackPanel _skillsView = null!;
    StackPanel _slash = null!;
    List<(string name, string desc, bool on)> _skillNames = new();
    string _skillsDir = "";

    ScrollViewer? _skillsScroll, _settingsScroll;

    // Each tab's scroller is made once: a panel can only ever have one parent, so wrapping it in a fresh
    // ScrollViewer on every visit threw "The Control already has a parent" the second time round.
    Control TabBody(string key) => key switch
    {
        "chat" => _chatView,
        "skills" => _skillsScroll ??= Ui.Scroll(_skillsView, new Thickness(0, 0, 16, 24)),
        _ => _settingsScroll ??= Ui.Scroll(_settingsView, new Thickness(0, 0, 0, 24)),
    };

    string WsQuery => _ws.Length > 0 ? "?ws=" + Api.Q(_ws) : "";

    async Task RefreshSkillNames()
    {
        var j = await Ops.Get("/api/agent/skills" + WsQuery, false);
        if (j == null) return;
        _skillNames = j.A("items").Items().Select(s => (s.S("name"), s.S("description"), s.B("enabled"))).ToList();
        _skillsDir = j.S("yours");
    }

    // ================================================================ the list

    async Task LoadSkills()
    {
        if (_skillsView.Children.Count == 0) _skillsView.Children.Add(Ui.Empty("loading skills…", "puzzle"));
        var j = await Ops.Get("/api/agent/skills" + WsQuery);
        if (j == null) return;
        _skillNames = j.A("items").Items().Select(s => (s.S("name"), s.S("description"), s.B("enabled"))).ToList();
        _skillsDir = j.S("yours");
        _skillsView.Children.Clear();

        var actions = Ui.H(8,
            Ui.Btn("New skill", () => ShowSkillEditor(null, ""), "plus", "brand"),
            Ui.Btn("Import…", () => _ = ImportSkill(false), "download", "", "A .zip or .skill file, or a SKILL.md"),
            Ui.Btn("Import folder…", () => _ = ImportSkill(true), "folder", "", "A folder with a SKILL.md in it"),
            Ui.IconBtn("folder", "Open your skills folder", () =>
            {
                try { Directory.CreateDirectory(_skillsDir); } catch (Exception) { }
                MainWindow.OpenUrl(_skillsDir);
            }, "", 38));
        _skillsView.Children.Add(Ui.Split(Ui.V(2, Ui.H1("Skills"),
            Ui.Hint("Instructions the assistant loads when a request matches them - or start one yourself by typing /name in the chat.")), actions));

        var items = j.A("items").Items().ToList();
        var groups = new (string key, string title, string sub)[]
        {
            ("workspace", "This workspace", "From .lodestone/skills or .claude/skills in the workspace folder."),
            ("yours", "Yours", "In " + _skillsDir + " - made here, imported, or saved by the assistant when you asked."),
            ("claude", "From Claude Code", "Your ~/.claude/skills folder - the same format works here unchanged."),
            ("built-in", "Built in", "Shipped with Lodestone. To change one, copy it to yours - your copy is used instead."),
        };
        foreach (var (key, title, sub) in groups)
        {
            var list = items.Where(s => s.S("source") == key).ToList();
            if (list.Count == 0 && key != "yours") continue;
            var rows = Ui.V(8);
            foreach (var s in list) rows.Children.Add(SkillRow(s));
            if (list.Count == 0)
                rows.Children.Add(Ui.Hint("None yet. Make one with New skill, import one, or ask the assistant: \"make a skill that…\"."));
            _skillsView.Children.Add(Ui.Card(title, sub, null, rows));
        }
        _skillsView.Children.Add(Ui.Card("How a skill is made", null, null, Ui.V(8,
            Ui.T("A folder with a SKILL.md: a short header naming it and saying when to use it, then the instructions. " +
                 "Files beside SKILL.md (references, templates, examples) are read when the instructions mention them.", null, 13, Ui.Text, wrap: true),
            Ui.Code("---\nname: my-skill\ndescription: What it does, and when to use it.\n---\n\n# Instructions\n1. ...\n2. ...", 12))));
    }

    Control SkillRow(JsonNode s)
    {
        var name = s.S("name");
        var yours = s.S("source") == "yours";
        var on = Ui.Switch(null, s.B("enabled"), v => _ = ToggleSkill(name, v));
        ToolTip.SetTip(on, "Let the assistant use it");
        var title = Ui.H(8, Ui.T("/" + name, null, 14, Ui.Contrast, FontWeight.SemiBold).Also(t => t.FontFamily = Ui.Mono),
            s.B("user_only") ? Ui.Tag("only when you start it") : null,
            s.B("valid_name") ? null : Ui.Tag("unusual name", null, Ui.Orange));
        var desc = Ui.T(s.S("description"), null, 12.5, Ui.Secondary, wrap: true);
        var buttons = Ui.H(4,
            Ui.Btn("Try", () => TrySkill(name), "play", "ghost xs", "Start it in the chat"),
            Ui.Btn(yours ? "Edit" : "View", () => _ = OpenSkill(name, yours), yours ? "edit" : "eye", "ghost xs"),
            yours ? Ui.IconBtn("trash", "Delete", () => _ = DeleteSkill(name), "ghost xs", 28) : null);
        var row = Ui.Row(Ui.Lead(on, Ui.V(3, title, desc), 12), buttons);
        return row;
    }

    async Task ToggleSkill(string name, bool on)
    {
        await Ops.Act("/api/agent/skills/toggle", new JsonObject { ["name"] = name, ["enabled"] = on }, null);
        await RefreshSkillNames();
    }

    void TrySkill(string name)
    {
        OpenTab("chat");
        _input.Text = "/" + name + " ";
        _input.CaretIndex = _input.Text.Length;
        _input.Focus();
    }

    async Task DeleteSkill(string name)
    {
        if (!await Dialogs.Confirm("Delete skill", "Delete the skill " + name + " and its folder?", "Delete")) return;
        if (await Ops.Act("/api/agent/skills/delete", new JsonObject { ["name"] = name }, "Deleted.") != null) await LoadSkills();
    }

    async Task OpenSkill(string name, bool yours)
    {
        var j = await Ops.Get("/api/agent/skill?name=" + Api.Q(name) + (_ws.Length > 0 ? "&ws=" + Api.Q(_ws) : ""));
        if (j == null) return;
        if (yours) ShowSkillEditor(j.N("skill"), j.S("body"));
        else ShowSkillViewer(j);
    }

    void ShowSkillViewer(JsonNode j)
    {
        var s = j.N("skill");
        var files = j.Strings("files");
        var card = Ui.Card("/" + s.S("name"), s.S("description"),
            Ui.H(8,
                Ui.Btn("Copy to my skills", () => ShowSkillEditor(null, j.S("body"), s.S("name"), s.S("description")), "copy", "sm",
                    "Your copy is used instead of this one, and you can change it"),
                Ui.Btn("Close", () => _ = LoadSkills(), null, "ghost sm")),
            Ui.V(10,
                Ui.Hint("From " + s.S("dir")),
                Markdown.Render(j.S("body")),
                files.Count > 0 ? Ui.V(4, Ui.Label("Files that come with it"), Ui.MonoText(string.Join("\n", files), 12, Ui.Secondary)) : null));
        _skillsView.Children.Insert(1, card);
    }

    // ================================================================ writing one

    void ShowSkillEditor(JsonNode? existing, string body, string? name = null, string? desc = null)
    {
        var old = existing?.S("name") ?? "";
        var nameIn = Ui.Input(existing?.S("name") ?? name ?? "", "my-skill", mono: true, width: 320);
        nameIn.HorizontalAlignment = HorizontalAlignment.Left;
        var descIn = Ui.Area(existing?.S("description") ?? desc ?? "", 2,
            "What it does, and when to use it - in the words you would ask with. The assistant picks skills by this.");
        var bodyIn = Ui.Area(body, 16, "The instructions, in Markdown: the steps, the tools to use, how to check the result, what to avoid.", true);
        bodyIn.MaxHeight = 520;
        Border card = null!;
        card = Ui.Card(old.Length > 0 ? "Edit /" + old : "New skill", "Saved as SKILL.md in your skills folder.",
            Ui.H(8,
                Ui.Btn("Save", async () =>
                {
                    var res = await Ops.Act("/api/agent/skills/save", new JsonObject
                    {
                        ["name"] = (nameIn.Text ?? "").Trim(), ["description"] = descIn.Text ?? "", ["body"] = bodyIn.Text ?? "",
                        ["old"] = old, ["overwrite"] = old.Length > 0,
                    }, "Saved.");
                    if (res != null) await LoadSkills();
                }, "save", "brand sm"),
                Ui.Btn("Cancel", () => _skillsView.Children.Remove(card), null, "ghost sm")),
            Ui.V(10,
                Ui.Field("Name", nameIn, "Lowercase words joined by hyphens - you start it with /name."),
                Ui.Field("Description", descIn),
                Ui.Field("Instructions", bodyIn)));
        _skillsView.Children.Insert(1, card);
        nameIn.Focus();
    }

    async Task ImportSkill(bool folder)
    {
        string? path = null;
        if (folder)
        {
            var picked = await S.StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions
            {
                Title = "A skill folder (with a SKILL.md in it)",
                AllowMultiple = false,
            });
            path = picked.FirstOrDefault()?.TryGetLocalPath();
        }
        else
        {
            var picked = await S.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
            {
                Title = "A skill: .zip, .skill or SKILL.md",
                AllowMultiple = false,
                FileTypeFilter = new[]
                {
                    new FilePickerFileType("Skills") { Patterns = new[] { "*.zip", "*.skill", "SKILL.md" } },
                    FilePickerFileTypes.All,
                },
            });
            path = picked.FirstOrDefault()?.TryGetLocalPath();
        }
        if (string.IsNullOrEmpty(path)) return;
        JsonNode res;
        try { res = await Api.Post("/api/agent/skills/import", new JsonObject { ["path"] = path }); }
        catch (Exception e)
        {
            if (!e.Message.Contains("already have a skill"))
            {
                Toasts.Bad(e.Message);
                return;
            }
            if (!await Dialogs.Confirm("Replace skill?", e.Message + " Replace it with this one?", "Replace")) return;
            try { res = await Api.Post("/api/agent/skills/import", new JsonObject { ["path"] = path, ["overwrite"] = true }); }
            catch (Exception e2)
            {
                Toasts.Bad(e2.Message);
                return;
            }
        }
        Toasts.Show("Added the skill " + res.Path("skill.name")?.ToString() + ".", "on");
        await LoadSkills();
    }

    // ================================================================ in the chat

    /// <summary>Typing "/" at the start of a message lists the skills that match.</summary>
    void PaintSlash()
    {
        var t = _input.Text ?? "";
        _slash.Children.Clear();
        if (!t.StartsWith('/') || t.Contains(' ') || t.Contains('\n'))
        {
            _slash.IsVisible = false;
            return;
        }
        var q = t[1..].ToLowerInvariant();
        var hits = _skillNames.Where(s => s.on && s.name.Contains(q)).OrderBy(s => !s.name.StartsWith(q)).Take(6).ToList();
        foreach (var (name, desc, _) in hits)
        {
            var n = name;
            var b = new Button
            {
                Content = Ui.H(10, Ui.T("/" + name, null, 13, Ui.Contrast, FontWeight.SemiBold).Also(x => x.FontFamily = Ui.Mono),
                    Ui.T(desc, null, 12, Ui.Secondary).Also(x => x.TextTrimming = TextTrimming.CharacterEllipsis)),
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Left,
            };
            b.Classes.Add("nav");
            b.Click += (_, _) =>
            {
                _input.Text = "/" + n + " ";
                _input.CaretIndex = _input.Text.Length;
                _input.Focus();
            };
            _slash.Children.Add(b);
        }
        _slash.IsVisible = hits.Count > 0;
    }

    void SkillNote(JsonNode ev)
    {
        var chip = new Border
        {
            Background = Ui.BlueHighlight,
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(10, 5),
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(4, 0),
            Child = Ui.H(8, Icons.Make("puzzle", 14, Ui.Blue),
                Ui.T("Skill /" + ev.S("name"), null, 12.5, Ui.Blue, FontWeight.SemiBold),
                Ui.T(ev.S("how") == "you" ? "started by you" : "picked by the assistant", null, 12, Ui.Secondary)),
        };
        ToolTip.SetTip(chip, ev.S("description"));
        _msgs.Children.Add(chip);
        ScrollEnd();
    }
}
