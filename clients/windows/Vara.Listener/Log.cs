namespace Vara.Listener;

/// <summary>
/// A small file log at %LOCALAPPDATA%\VARA\logs\listener.log. It records events, never audio or what was
/// said: transcripts and replies stay in the backend's conversation history, where you can delete them.
/// </summary>
public static class Log
{
    private static readonly object Gate = new();
    private const long MaxBytes = 2 * 1024 * 1024;

    public static string FilePath { get; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "VARA", "logs", "listener.log");

    /// <summary>Off in tests, so they don't write to the real log.</summary>
    public static bool Enabled { get; set; } = true;

    public static void Info(string message) => Write("INFO", message);

    public static void Warn(string message) => Write("WARN", message);

    public static void Error(string message, Exception? ex = null) =>
        Write("ERROR", ex is null ? message : $"{message}: {ex.GetType().Name}: {ex.Message}");

    private static void Write(string level, string message)
    {
        if (!Enabled)
        {
            return;
        }
        try
        {
            lock (Gate)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
                if (File.Exists(FilePath) && new FileInfo(FilePath).Length > MaxBytes)
                {
                    File.Move(FilePath, FilePath + ".1", overwrite: true);
                }
                File.AppendAllText(FilePath, $"{DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss.fff} {level} {message}{Environment.NewLine}");
            }
        }
        catch
        {
            // Logging must never take the listener down.
        }
    }
}
