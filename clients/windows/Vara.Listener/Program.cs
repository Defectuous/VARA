namespace Vara.Listener;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        // One listener per Windows session: two would both answer.
        using var single = new Mutex(initiallyOwned: true, @"Local\VaraListener", out var first);
        if (!first)
        {
            return;
        }

        ApplicationConfiguration.Initialize();
        Application.ThreadException += (_, e) => Log.Error("Unhandled UI error", e.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, e) => Log.Error("Unhandled error", e.ExceptionObject as Exception);

        var settings = ListenerSettings.Load(Path.Combine(AppContext.BaseDirectory, "appsettings.json"));
        Application.Run(new TrayApp(settings));
    }
}
