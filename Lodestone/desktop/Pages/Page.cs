using System.Text.Json.Nodes;
using Avalonia.Controls;

namespace Lodestone.Desktop.Pages;

/// <summary>
/// One tab of the panel. The view is built on first use; OnShow reloads its
/// data each time the tab is opened, as the old page did.
/// </summary>
public abstract class Page
{
    protected readonly MainWindow S;

    protected Page(MainWindow shell) => S = shell;

    public abstract string Key { get; }
    public abstract string Title { get; }
    public abstract string Icon { get; }

    /// <summary>Shown under the server header; false for panel-wide tabs.</summary>
    public virtual bool ServerScoped => true;

    /// <summary>The heading it sits under in the sidebar.</summary>
    public virtual string Group => ServerScoped ? "Server" : "Lodestone";

    /// <summary>Hidden in Simple mode.</summary>
    public virtual bool Advanced => false;

    Control? _view;
    public Control View => _view ??= Build();
    public bool Built => _view != null;

    protected abstract Control Build();

    public virtual Task OnShow() => Task.CompletedTask;
    public virtual void OnHide() { }
    public virtual void OnStatus(JsonNode s) { }
    public virtual void OnServerSwitched() { }

    protected bool Showing => S.Current == Key;
}
