using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.VisualTree;
using AvaloniaEdit;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// The server's files, local or over SFTP: a breadcrumb bar, a table that
/// reveals row actions on hover, and a real code editor (line numbers, tab
/// indenting, Ctrl+S) beside it. Files dropped onto the window upload into the
/// folder on screen.
/// </summary>
public sealed class FilesPage : Page
{
    public FilesPage(MainWindow s) : base(s) { }

    public override string Key => "files";
    public override string Title => "Files";
    public override string Icon => "folder";

    string _cwd = "";
    string? _editRel;
    JsonNode? _editMtime;
    bool _loading;

    StackPanel _crumbs = null!;
    StackPanel _rows = null!;
    Grid _split = null!;
    Border _listCard = null!;
    Border _editorCard = null!;
    TextEditor _ed = null!;
    TextBlock _edName = null!;
    Border _edState = null!;
    Button _wrapBtn = null!, _maxBtn = null!;
    Border _drop = null!;
    bool _max;
    Grid _head = null!;
    JsonNode? _last;

    // Beside the editor the table is half as wide, so Modified is dropped
    // rather than letting the columns crush the names.
    bool Compact => _editorCard.IsVisible && !_max;
    ColumnDefinitions Cols() => new(Compact ? "*,84,0,132" : "*,100,150,132");

    protected override Control Build()
    {
        _crumbs = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 2, VerticalAlignment = VerticalAlignment.Center };
        var tools = Ui.H(8,
            Ui.Btn("New file", () => MkNew("file"), "file-plus", "sm"),
            Ui.Btn("New folder", () => MkNew("folder"), "folder-plus", "sm"),
            Ui.Btn("Upload", Upload, "upload", "sm brand"),
            Ui.IconBtn("refresh", "Refresh", () => _ = Load(_cwd), "sm", 32));
        var bar = Ui.Card(Ui.Split(_crumbs, tools), 12);
        bar.Margin = new Thickness(0, 0, 0, 16);

        // table
        var head = _head = new Grid { ColumnDefinitions = new ColumnDefinitions("*,100,150,132"), Margin = new Thickness(18, 12, 16, 8) };
        head.Children.Add(Ui.Eyebrow("Name"));
        head.Children.Add(Ui.Eyebrow("Size").Also(t => Grid.SetColumn(t, 1)));
        head.Children.Add(Ui.Eyebrow("Modified").Also(t => { Grid.SetColumn(t, 2); t.ClipToBounds = true; }));
        _rows = new StackPanel { Margin = new Thickness(8, 0, 8, 10) };
        var listBody = new ScrollViewer { Content = _rows, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
        var listGrid = new Grid { RowDefinitions = new RowDefinitions("Auto,Auto,*") };
        listGrid.Children.Add(head);
        var line = Ui.Divider1(0);
        Grid.SetRow(line, 1);
        listGrid.Children.Add(line);
        Grid.SetRow(listBody, 2);
        listGrid.Children.Add(listBody);

        _drop = new Border
        {
            Background = Ui.Hex("#261BD96A"),
            BorderBrush = Ui.Brand,
            BorderThickness = new Thickness(2),
            CornerRadius = new CornerRadius(16),
            IsVisible = false,
            IsHitTestVisible = false,
            Child = Ui.V(10, Icons.Make("upload", 34, Ui.Brand).Also(i => i.HorizontalAlignment = HorizontalAlignment.Center),
                Ui.T("Drop to upload here", null, 16, Ui.Brand, FontWeight.Bold))
                .Also(v => { v.HorizontalAlignment = HorizontalAlignment.Center; v.VerticalAlignment = VerticalAlignment.Center; }),
        };
        var listStack = new Grid();
        listStack.Children.Add(listGrid);
        listStack.Children.Add(_drop);
        _listCard = new Border { Child = listStack, ClipToBounds = true };
        _listCard.Classes.Add("card");

        // editor
        _ed = new TextEditor
        {
            ShowLineNumbers = true,
            FontFamily = Ui.Mono,
            FontSize = S.Prefs.Mono,
            Background = Ui.Terminal,
            Foreground = Ui.Hex("#DCE2E8"),
            LineNumbersForeground = Ui.Hex("#5B6370"),
            WordWrap = S.Prefs.Wrap,
            Padding = new Thickness(8, 8, 8, 8),
        };
        _ed.Options.IndentationSize = 4;
        _ed.Options.ConvertTabsToSpaces = false;
        _ed.Options.EnableHyperlinks = false;
        _ed.Options.EnableEmailHyperlinks = false;
        _ed.Options.HighlightCurrentLine = true;
        _ed.TextArea.TextView.CurrentLineBackground = Ui.Hex("#15171B");
        _ed.TextArea.TextView.CurrentLineBorder = new Pen(Ui.Hex("#1D1F23"));
        _ed.TextArea.SelectionBrush = Ui.Hex("#551BD96A");
        _ed.TextArea.SelectionForeground = null;
        _ed.TextArea.Caret.CaretBrush = Ui.Brand;
        _ed.TextChanged += (_, _) =>
        {
            if (!_loading) Ui.SetPill(_edState, "unsaved", "warn");
        };
        _ed.AddHandler(InputElement.KeyDownEvent, EdKey, Avalonia.Interactivity.RoutingStrategies.Tunnel);

        _edName = Ui.H2("file");
        _edName.TextTrimming = TextTrimming.CharacterEllipsis;
        _edState = Ui.Pill("saved");
        _wrapBtn = Ui.IconBtn("wrap", "Wrap long lines", ToggleWrap, "ghost sm", 32);
        _maxBtn = Ui.IconBtn("maximize", "Expand the editor", ToggleMax, "ghost sm", 32);
        var edHead = Ui.Split(Ui.H(10, Icons.Make("file-text", 20, Ui.Secondary), _edName, _edState),
            Ui.H(6, _wrapBtn, _maxBtn,
                Ui.Btn("Save", () => _ = Save(), "save", "brand sm"),
                Ui.IconBtn("x", "Close", CloseEdit, "ghost sm", 32)));
        var edBody = new Border
        {
            Child = _ed,
            BorderBrush = Ui.Divider,
            BorderThickness = new Thickness(0, 1, 0, 0),
        };
        _editorCard = Ui.FillCard(edHead, edBody);
        _editorCard.IsVisible = false;

        _split = new Grid { ColumnDefinitions = new ColumnDefinitions("*,0,0") };
        _split.Children.Add(_listCard);
        Grid.SetColumn(_editorCard, 2);
        _split.Children.Add(_editorCard);

        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,*"), Margin = new Thickness(28, 16, 28, 24) };
        root.Children.Add(bar);
        Grid.SetRow(_split, 1);
        root.Children.Add(_split);

        DragDrop.SetAllowDrop(root, true);
        root.AddHandler(DragDrop.DragEnterEvent, (_, e) => { _drop.IsVisible = true; e.DragEffects = DragDropEffects.Copy; });
        root.AddHandler(DragDrop.DragOverEvent, (_, e) => e.DragEffects = DragDropEffects.Copy);
        root.AddHandler(DragDrop.DragLeaveEvent, (_, _) => _drop.IsVisible = false);
        root.AddHandler(DragDrop.DropEvent, OnDrop);

        S.SizesChanged += () => _ed.FontSize = S.Prefs.Mono;
        UpdateWrapButton();
        return root;
    }

    public override Task OnShow() => Load(_cwd);

    /// <summary>For --shots.</summary>
    public Task OpenFile(string rel) => Open(rel);

    public override void OnServerSwitched()
    {
        _cwd = "";
        if (Built) CloseEdit();
    }

    // ================================================================ list

    async Task Load(string rel)
    {
        JsonNode d;
        try
        {
            d = await Api.Get("/api/files?path=" + Api.Q(rel));
        }
        catch (Exception e)
        {
            _rows.Children.Clear();
            _rows.Children.Add(Ui.Empty(e.Message, "alert"));
            return;
        }
        _cwd = d.S("rel");
        _last = d;
        RenderCrumbs();
        RenderRows();
    }

    void RenderRows()
    {
        var d = _last;
        if (d == null) return;
        _head.ColumnDefinitions = Cols();
        _rows.Children.Clear();
        var parts = _cwd.Length > 0 ? _cwd.Split('/') : Array.Empty<string>();
        if (!d.B("at_root"))
        {
            var up = string.Join("/", parts.Take(parts.Length - 1));
            _rows.Children.Add(FileRow("..", up, true, "", "", true));
        }
        foreach (var i in d.A("items").Items())
            _rows.Children.Add(FileRow(i.S("name"), i.S("rel"), i.B("dir"), i.S("size"), i.S("mod"), false));
        if (_rows.Children.Count == 0)
            _rows.Children.Add(Ui.Empty("This folder is empty. Drop files here to upload them.", "folder"));
    }

    void RenderCrumbs()
    {
        _crumbs.Children.Clear();
        Button Crumb(string label, string rel, bool home = false)
        {
            var b = home ? Ui.Btn(label, () => _ = Load(rel), "server", "ghost sm") : Ui.Btn(label, () => _ = Load(rel), null, "ghost sm");
            b.FontFamily = Ui.Mono;
            return b;
        }
        _crumbs.Children.Add(Crumb("server root", "", true));
        var acc = "";
        foreach (var p in _cwd.Split('/', StringSplitOptions.RemoveEmptyEntries))
        {
            acc = acc.Length > 0 ? acc + "/" + p : p;
            _crumbs.Children.Add(Icons.Make("chevron-right", 14, Ui.Secondary));
            _crumbs.Children.Add(Crumb(p, acc));
        }
    }

    Control FileRow(string name, string rel, bool dir, string size, string mod, bool up)
    {
        var g = new Grid { ColumnDefinitions = Cols(), MinHeight = 40 };
        var icon = Icons.Make(up ? "arrow-up" : dir ? "folder" : "file", 18, dir ? Ui.Brand : Ui.Secondary);
        var label = Ui.T(name, null, 14, dir ? Ui.Contrast : Ui.Text, dir ? FontWeight.SemiBold : FontWeight.Normal);
        label.TextTrimming = TextTrimming.CharacterEllipsis;
        ToolTip.SetTip(label, name);
        var nameCell = Ui.Lead(icon, label, 11);
        nameCell.Margin = new Thickness(10, 0, 8, 0);
        g.Children.Add(nameCell);
        g.Children.Add(Ui.MonoText(size, 12.5, Ui.Secondary).Also(t => { t.VerticalAlignment = VerticalAlignment.Center; Grid.SetColumn(t, 1); }));
        g.Children.Add(Ui.MonoText(mod, 12.5, Ui.Secondary).Also(t => { t.VerticalAlignment = VerticalAlignment.Center; t.ClipToBounds = true; Grid.SetColumn(t, 2); }));

        StackPanel? ops = null;
        if (!up)
        {
            ops = Ui.H(2,
                Ui.IconBtn("download", "Download", () => _ = Dl(rel, name, dir), "ghost", 30),
                Ui.IconBtn("edit", "Rename", () => Rename(rel, name), "ghost", 30),
                Ui.IconBtn("copy", "Duplicate", () => _ = CopyIt(rel), "ghost", 30),
                Ui.IconBtn("trash", "Delete", () => Delete(rel, name), "soft-red", 30));
            ops.HorizontalAlignment = HorizontalAlignment.Right;
            ops.Opacity = 0;
            Grid.SetColumn(ops, 3);
            g.Children.Add(ops);
        }
        var row = new Border
        {
            Child = g,
            CornerRadius = new CornerRadius(10),
            Padding = new Thickness(0, 2, 8, 2),
            Background = Brushes.Transparent,
            Cursor = new Cursor(StandardCursorType.Hand),
        };
        row.PointerEntered += (_, _) => { row.Background = Ui.RowBg; if (ops != null) ops.Opacity = 1; };
        row.PointerExited += (_, _) => { row.Background = Brushes.Transparent; if (ops != null) ops.Opacity = 0; };
        row.PointerPressed += (_, e) =>
        {
            if (e.Source is Visual v && v.FindAncestorOfType<Button>(true) != null) return;
            if (!e.GetCurrentPoint(row).Properties.IsLeftButtonPressed) return;
            if (dir) _ = Load(rel);
            else _ = Open(rel);
        };
        return row;
    }

    // ================================================================ editor

    async Task Open(string rel)
    {
        try
        {
            var j = await Api.Get("/api/file?path=" + Api.Q(rel));
            _editRel = rel;
            _editMtime = j.N("mtime")?.DeepClone();
            _loading = true;
            _ed.Text = j.S("content");
            _ed.ScrollToHome();
            _loading = false;
            _edName.Text = rel.Split('/').Last();
            ToolTip.SetTip(_edName, rel);
            Ui.SetPill(_edState, "saved", "");
            ShowEditor(true);
            _ed.TextArea.Focus();
        }
        catch (Exception e)
        {
            _loading = false;
            Toasts.Bad(e.Message);
        }
    }

    void ShowEditor(bool on)
    {
        _editorCard.IsVisible = on;
        if (!on)
        {
            _max = false;
            _split.ColumnDefinitions = new ColumnDefinitions("*,0,0");
            _listCard.IsVisible = true;
        }
        else if (_max)
        {
            _split.ColumnDefinitions = new ColumnDefinitions("0,0,*");
            _listCard.IsVisible = false;
        }
        else
        {
            _split.ColumnDefinitions = new ColumnDefinitions("*,16,1.15*");
            _listCard.IsVisible = true;
        }
        _maxBtn.Content = Icons.Make(_max ? "minimize" : "maximize", 18);
        RenderRows();
        ToolTip.SetTip(_maxBtn, _max ? "Shrink the editor" : "Expand the editor");
    }

    void ToggleMax()
    {
        _max = !_max;
        ShowEditor(true);
    }

    void ToggleWrap()
    {
        S.Prefs.Wrap = !S.Prefs.Wrap;
        _ed.WordWrap = S.Prefs.Wrap;
        S.Prefs.Save();
        UpdateWrapButton();
    }

    void UpdateWrapButton()
    {
        _wrapBtn.Classes.Set("ghost", !S.Prefs.Wrap);
        _wrapBtn.Classes.Set("soft", S.Prefs.Wrap);
    }

    void CloseEdit()
    {
        _editRel = null;
        ShowEditor(false);
    }

    void EdKey(object? sender, KeyEventArgs e)
    {
        var mod = e.KeyModifiers.HasFlag(KeyModifiers.Control) || e.KeyModifiers.HasFlag(KeyModifiers.Meta);
        if (!mod) return;
        if (e.Key == Avalonia.Input.Key.S) { e.Handled = true; _ = Save(); }
        else if (e.Key is Avalonia.Input.Key.OemPlus or Avalonia.Input.Key.Add) { e.Handled = true; S.BumpMono(1); }
        else if (e.Key is Avalonia.Input.Key.OemMinus or Avalonia.Input.Key.Subtract) { e.Handled = true; S.BumpMono(-1); }
    }

    async Task Save(bool force = false)
    {
        if (_editRel == null) return;
        var body = new JsonObject { ["path"] = _editRel, ["content"] = _ed.Text };
        if (!force) body["mtime"] = _editMtime?.DeepClone();
        try
        {
            var j = await Api.Post("/api/file/save", body);
            _editMtime = j.N("mtime")?.DeepClone();
            Ui.SetPill(_edState, "saved", "on");
            Toasts.Good(force ? "Overwritten." : "Saved.");
        }
        catch (ApiException e) when (e.Message.Contains("changed on", StringComparison.OrdinalIgnoreCase) || e.Status == 409)
        {
            if (await Dialogs.Confirm("File changed underneath you", e.Message + " Overwrite it with what is in the editor?", "Overwrite"))
                await Save(true);
        }
        catch (Exception e)
        {
            Toasts.Bad(e.Message);
        }
    }

    // ================================================================ file ops

    async Task Dl(string rel, string name, bool dir)
    {
        var path = (dir ? "/api/download/folder?path=" : "/api/download?path=") + Api.Q(rel);
        await S.SaveDownload(path, dir ? name + ".zip" : name);
    }

    async void MkNew(string kind)
    {
        var name = await Dialogs.Prompt("New " + kind, "Name", "", "Create");
        if (string.IsNullOrWhiteSpace(name)) return;
        await Ops.Act("/api/file/" + (kind == "folder" ? "mkdir" : "new"), new { path = _cwd, name }, kind + " created.");
        await Load(_cwd);
    }

    async void Rename(string rel, string cur)
    {
        var name = await Dialogs.Prompt("Rename", "New name", cur, "Rename");
        if (string.IsNullOrWhiteSpace(name) || name == cur) return;
        await Ops.Act("/api/file/rename", new { path = rel, name }, "Renamed.");
        await Load(_cwd);
    }

    async Task CopyIt(string rel)
    {
        await Ops.Act("/api/file/copy", new { path = rel }, "Copied.");
        await Load(_cwd);
    }

    async void Delete(string rel, string name)
    {
        if (!await Dialogs.Confirm("Delete " + name + "?", "This cannot be undone from the panel.", "Delete")) return;
        await Ops.Act("/api/file/delete", new { path = rel }, "Deleted.");
        if (_editRel == rel) CloseEdit();
        await Load(_cwd);
    }

    async void Upload()
    {
        var files = await S.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
        {
            Title = "Upload into /" + _cwd,
            AllowMultiple = true,
        });
        await UploadAll(files);
    }

    async void OnDrop(object? sender, DragEventArgs e)
    {
        _drop.IsVisible = false;
#pragma warning disable CS0618 // IDataObject is what 11.x hands drop handlers
        var items = e.Data.GetFiles()?.ToList() ?? new List<IStorageItem>();
#pragma warning restore CS0618
        var files = items.OfType<IStorageFile>().ToList();
        if (items.Count > files.Count)
            Toasts.Show("Folders are skipped - zip them first, or upload their files.");
        await UploadAll(files);
    }

    async Task UploadAll(IReadOnlyList<IStorageFile> files)
    {
        if (files.Count == 0) return;
        foreach (var f in files)
        {
            try
            {
                await using var stream = await f.OpenReadAsync();
                await Api.PostRaw("/api/upload?path=" + Api.Q(_cwd) + "&name=" + Api.Q(f.Name), stream);
                Toasts.Good("Uploaded " + f.Name);
            }
            catch (Exception ex)
            {
                Toasts.Bad(f.Name + ": " + ex.Message);
            }
        }
        await Load(_cwd);
    }
}
