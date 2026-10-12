using Avalonia;
using Avalonia.Headless;
using Avalonia.Threading;
using Lodestone.Desktop.Core;
using Lodestone.Desktop.UI;

namespace Lodestone.Desktop;

/// <summary>
/// --shots DIR: render every page to a PNG with no screen, against an engine
/// that is already running. This is how the look is checked on a machine
/// that will not run a freshly built exe.
/// </summary>
public static class Shots
{
    public static int Run(Options opt)
    {
        AppBuilder.Configure<App>()
            .UseSkia()
            .WithInterFont()
            .UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false })
            .SetupWithoutStarting();
        if (SynchronizationContext.Current == null)
            SynchronizationContext.SetSynchronizationContext(new AvaloniaSynchronizationContext());

        var bad = Icons.Broken();
        if (bad.Count > 0) System.Console.WriteLine("BROKEN ICONS: " + string.Join(", ", bad));

        Directory.CreateDirectory(opt.ShotsDir!);
        opt.AttachOnly = true;
        var w = new MainWindow(opt) { Width = 1440, Height = 900 };
        w.Show();
        Pump(300);

        var t = Run(w, opt);
        var until = DateTime.UtcNow.AddMinutes(4);
        while (!t.IsCompleted && DateTime.UtcNow < until) Pump(50);
        if (t.IsFaulted) System.Console.WriteLine("FAILED: " + t.Exception);
        return t.IsCompletedSuccessfully ? 0 : 1;
    }

    static async Task Run(MainWindow w, Options opt)
    {
        await w.Engine.Connect(opt, new Progress<string>(_ => { }));
        await w.AfterConnect();
        var only = Environment.GetEnvironmentVariable("SHOTS_ONLY");
        foreach (var key in new[] { "servers", "console", "files", "props", "backups", "content", "instance", "tools", "auto", "ai", "discord", "network", "api", "settings" })
        {
            if (!string.IsNullOrEmpty(only) && !only.Split(',').Contains(key)) continue;
            w.ShowView(key);
            await Settle(2500);
            Save(w, key);
        }
        if (string.IsNullOrEmpty(only) || only.Split(',').Contains("extra"))
        {
            w.ShowView("tools");
            var tools = (Pages.ToolsPage)w.GetPage("tools")!;
            foreach (var tab in new[] { "worlds", "plugins", "lists", "ram" })
            {
                await tools.OpenTab(tab);
                await Settle(1500);
                Save(w, "tools-" + tab);
            }
            w.ShowView("files");
            await Settle(800);
            await ((Pages.FilesPage)w.GetPage("files")!).OpenFile("server.properties");
            await Settle(1000);
            Save(w, "files-editor");
            var lib = (Pages.ServersPage)w.GetPage("servers")!;
            w.ShowView("console");
            await Settle(300);
            w.ShowView("servers");
            await lib.ShowTab("profiles");
            await Settle(1200);
            Save(w, "servers-profiles");
            await lib.ShowTab("instances", "list");
            await Settle(1200);
            Save(w, "servers-list");
            await lib.ShowTab("instances", "grid");
            lib.ShotWelcome();
            await Settle(1500);
            Save(w, "welcome");
            await lib.ShowTab("instances", "grid");
            _ = lib.AddExisting();
            await Settle(800);
            Save(w, "dialog-add");
            Dialogs.Close(null);
            _ = UI.IconPicker.Pick("block/loom", "block/loom");
            await Settle(3000);
            Save(w, "dialog-icons");
            Dialogs.Close(null);
            var folder = Environment.GetEnvironmentVariable("SHOTS_ADD_FOLDER");
            if (!string.IsNullOrEmpty(folder))
            {
                var adding = lib.AddFolder(folder);
                await Settle(2500);
                if (adding.IsFaulted) System.Console.WriteLine("ADD FOLDER FAILED: " + adding.Exception);
                Save(w, "dialog-add-folder");
                Dialogs.Close(null);
            }
        }
        if (string.IsNullOrEmpty(only) || only.Split(',').Contains("create"))
        {
            w.ShowView("content");
            var content = (Pages.ContentPage)w.GetPage("content")!;
            await content.OpenTab("browse");
            await Settle(6000);
            Save(w, "content-browse");
            var sid = w.State.N("server").S("id");
            _ = Pages.ContentBrowser.ChooseVersion("sid=" + Api.Q(sid),
                new System.Text.Json.Nodes.JsonObject { ["source"] = "modrinth", ["id"] = "waystones" }, sid, null, null);
            await Settle(7000);
            Save(w, "content-dialog");
            Dialogs.Close(null);
            await content.OpenTab("installed");
            await Settle(2500);
            Save(w, "content-installed");
            w.ShowView("servers");
            await Settle(800);
            var servers = (Pages.ServersPage)w.GetPage("servers")!;
            foreach (var (step, name) in new[] { (0, "create-software"), (1, "create-content"), (2, "create-settings"), (3, "create-review") })
            {
                await servers.ShotWizard(step, "fabric", "1.21.1");
                await Settle(step == 1 ? 7000 : 3000);
                Save(w, name);
            }
            await servers.ShotWizard(0, "paper", "1.21.4");
            await Settle(3000);
            Save(w, "create-software-paper");
        }
        w.ChooseMode();
        await Settle(600);
        Save(w, "dialog-mode");
    }

    static async Task Settle(int ms)
    {
        var until = DateTime.UtcNow.AddMilliseconds(ms);
        while (DateTime.UtcNow < until) await Task.Delay(50);
    }

    static void Save(MainWindow w, string name)
    {
        AvaloniaHeadlessPlatform.ForceRenderTimerTick();
        Dispatcher.UIThread.RunJobs();
        var frame = w.CaptureRenderedFrame();
        var path = Path.Combine(Program.Options.ShotsDir!, name + ".png");
        frame?.Save(path);
        System.Console.WriteLine("shot " + path);
    }

    static void Pump(int ms)
    {
        var until = DateTime.UtcNow.AddMilliseconds(ms);
        while (DateTime.UtcNow < until)
        {
            Dispatcher.UIThread.RunJobs();
            AvaloniaHeadlessPlatform.ForceRenderTimerTick();
            Thread.Sleep(15);
        }
    }
}
