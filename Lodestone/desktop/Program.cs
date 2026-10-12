using Avalonia;
using Lodestone.Desktop.Core;

namespace Lodestone.Desktop;

public static class Program
{
    public static Options Options { get; private set; } = new();

    [STAThread]
    public static int Main(string[] args)
    {
        Options = Options.Parse(args);
        if (Options.ShotsDir != null)
            return Shots.Run(Options);
        BuildAvaloniaApp().StartWithClassicDesktopLifetime(args);
        return 0;
    }

    // Also used by the XAML previewer.
    public static AppBuilder BuildAvaloniaApp() =>
        AppBuilder.Configure<App>()
            .UsePlatformDetect()
            .WithInterFont()
            .LogToTrace();
}
