using System.Diagnostics;
using Vara.Listener.Audio;
using Vara.Listener.Backend;
using Vara.Listener.Voice;
using Vara.Listener.WakeWord;

namespace Vara.Listener;

/// <summary>The tray icon and its menu. All listening happens on a background task in <see cref="VoiceLoop"/>.</summary>
public sealed class TrayApp : ApplicationContext
{
    private readonly ListenerSettings _settings;
    private readonly NotifyIcon _tray;
    private readonly ToolStripMenuItem _status = new("Starting…") { Enabled = false };
    private readonly ToolStripMenuItem _pause = new("Pause listening");
    private readonly SynchronizationContext _ui;
    private readonly CancellationTokenSource _stop = new();
    private Microphone? _microphone;
    private WakeWordDetector? _detector;
    private Speaker? _speaker;
    private BackendVoice? _voice;
    private VoiceLoop? _loop;

    public TrayApp(ListenerSettings settings)
    {
        _settings = settings;
        _ui = SynchronizationContext.Current ?? new WindowsFormsSynchronizationContext();

        var menu = new ContextMenuStrip();
        menu.Items.Add(_status);
        menu.Items.Add(new ToolStripSeparator());
        _pause.Click += (_, _) => TogglePause();
        menu.Items.Add(_pause);
        menu.Items.Add("Open chat", null, (_, _) => OpenChat());
        menu.Items.Add("Open log", null, (_, _) => OpenLog());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Quit", null, (_, _) => ExitThread());

        _tray = new NotifyIcon
        {
            Icon = LoadTrayIcon(),
            Text = "V.A.R.A.",
            ContextMenuStrip = menu,
            Visible = true,
        };
        _tray.DoubleClick += (_, _) => OpenChat();

        _ = StartAsync();
    }

    private async Task StartAsync()
    {
        Log.Info("Listener starting");
        var baseDir = AppContext.BaseDirectory;
        var wakeModel = Path.Combine(baseDir, _settings.WakeWordModel);
        if (!File.Exists(wakeModel))
        {
            Fail($"The wake-word model wasn't found ({_settings.WakeWordModel}). Train it, then rebuild the listener.");
            return;
        }

        var client = VaraClient.Create(_settings.BackendUrl);
        if (!await client.IsUpAsync())
        {
            Notify("The V.A.R.A. server isn't running. Start it with start-vara; listening will work once it's up.",
                ToolTipIcon.Warning);
        }

        try
        {
            _detector = new WakeWordDetector(
                Path.Combine(baseDir, "models", "melspectrogram.onnx"),
                Path.Combine(baseDir, "models", "embedding_model.onnx"),
                wakeModel);
            _speaker = new Speaker(_settings.FallbackVoice);
            _voice = new BackendVoice((text, ct) => client.SpeakAsync(text, _settings.Voice, ct), new WavePlayer(), _speaker);
        }
        catch (Exception ex)
        {
            Log.Error("Starting", ex);
            Fail($"The listener couldn't start: {ex.Message}");
            return;
        }

        _loop = new VoiceLoop(_settings, _detector, client, _voice, new Chimes());
        _loop.StateChanged += state => _ui.Post(_ => ShowState(state), null);
        _loop.Problem += message => _ui.Post(_ => Notify(message, ToolTipIcon.Warning), null);
        await ListenAsync();
    }

    /// <summary>
    /// Keeps the microphone open. If recording stops (sleep and resume, a USB microphone unplugged), it
    /// reopens it every few seconds; you're only told if it stays unavailable.
    /// </summary>
    private async Task ListenAsync()
    {
        var failingSince = (DateTimeOffset?)null;
        var told = false;
        while (!_stop.IsCancellationRequested)
        {
            using var micStopped = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
            try
            {
                _microphone = new Microphone(_settings.MicrophoneDevice);
                _microphone.Stopped += ex =>
                {
                    if (!_stop.IsCancellationRequested)
                    {
                        Log.Warn($"Microphone stopped{(ex is null ? "" : $": {ex.Message}")}; reopening it");
                        micStopped.Cancel();
                    }
                };
                _microphone.Start();
                if (told)
                {
                    _ui.Post(_ => Notify("The microphone is working again.", ToolTipIcon.Info), null);
                }
                failingSince = null;
                told = false;
                _ui.Post(_ =>
                {
                    _pause.Enabled = true;
                    ShowState(_loop!.State);
                }, null);
                Log.Info($"Listening (threshold {_settings.Threshold}, microphone {DescribeMicrophone()})");
                if (_settings.MicrophoneDevice < 0)
                {
                    _ = FollowDefaultMicrophoneAsync(Microphone.DefaultDeviceName(), micStopped);
                }
                await Task.Run(() => _loop!.RunAsync(_microphone.Chunks, micStopped.Token));
            }
            catch (OperationCanceledException)
            {
                // The microphone stopped, or the listener is quitting.
            }
            catch (Exception ex)
            {
                Log.Error("Opening the microphone", ex);
                failingSince ??= DateTimeOffset.Now;
                if (!told && DateTimeOffset.Now - failingSince > TimeSpan.FromSeconds(30))
                {
                    told = true;
                    Fail(ex is NAudio.MmException
                        ? "V.A.R.A. can't use the microphone. Check it's connected, and that Windows Settings > " +
                          "Privacy & security > Microphone allows desktop apps. It will keep trying."
                        : $"The microphone isn't working: {ex.Message}. It will keep trying.");
                }
            }
            finally
            {
                _microphone?.Dispose();
                _microphone = null;
            }
            if (!_stop.IsCancellationRequested)
            {
                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(3), _stop.Token);
                }
                catch (OperationCanceledException)
                {
                }
            }
        }
    }

    /// <summary>
    /// When you change the default microphone in Windows, switch to it: the open device would otherwise
    /// stay on the old one. Waits until V.A.R.A. isn't in the middle of a request.
    /// </summary>
    private async Task FollowDefaultMicrophoneAsync(string? opened, CancellationTokenSource reopen)
    {
        try
        {
            while (!reopen.IsCancellationRequested)
            {
                await Task.Delay(TimeSpan.FromSeconds(5), reopen.Token);
                var current = Microphone.DefaultDeviceName();
                var idle = _loop!.State is ListenerState.Listening or ListenerState.Paused;
                if (current is not null && current != opened && idle)
                {
                    Log.Info($"The Windows default microphone changed to {current}; switching to it");
                    reopen.Cancel();
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception ex)
        {
            Log.Warn($"Checking the default microphone: {ex.Message}");
        }
    }

    private void ShowState(ListenerState state)
    {
        var text = state switch
        {
            ListenerState.Listening => "Listening for \"VARA\"",
            ListenerState.Paused => "Paused",
            ListenerState.Capturing => "Listening to you…",
            ListenerState.Thinking => "Thinking…",
            ListenerState.Speaking => "Speaking",
            _ => state.ToString(),
        };
        _status.Text = text;
        _tray.Text = $"V.A.R.A. - {text}";
        _pause.Text = state == ListenerState.Paused ? "Resume listening" : "Pause listening";
    }

    private void TogglePause()
    {
        if (_loop is null)
        {
            return;
        }
        _loop.Paused = !_loop.Paused;
        Log.Info(_loop.Paused ? "Paused" : "Resumed");
    }

    private void Fail(string message)
    {
        _ui.Post(_ =>
        {
            _status.Text = "Not listening";
            _tray.Text = "V.A.R.A. - not listening";
            _pause.Enabled = false;
            Notify(message, ToolTipIcon.Error);
        }, null);
        Log.Warn(message);
    }

    private void Notify(string message, ToolTipIcon icon) =>
        _tray.ShowBalloonTip(8000, "V.A.R.A.", message, icon);

    private void OpenChat() =>
        Process.Start(new ProcessStartInfo(_settings.BackendUrl.TrimEnd('/') + "/") { UseShellExecute = true });

    private static void OpenLog()
    {
        if (File.Exists(Log.FilePath))
        {
            Process.Start(new ProcessStartInfo(Log.FilePath) { UseShellExecute = true });
        }
    }

    private string DescribeMicrophone()
    {
        if (_settings.MicrophoneDevice < 0)
        {
            return $"Windows default: {Microphone.DefaultDeviceName() ?? "unknown"}";
        }
        var names = Microphone.DeviceNames();
        return _settings.MicrophoneDevice < names.Count ? names[_settings.MicrophoneDevice] : $"#{_settings.MicrophoneDevice}";
    }

    private static Icon LoadTrayIcon()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "tray-icon.ico");
        // Ask for the size Windows uses in the notification area at the current display scaling.
        return File.Exists(path) ? new Icon(path, SystemInformation.SmallIconSize) : SystemIcons.Application;
    }

    protected override void ExitThreadCore()
    {
        Log.Info("Listener stopping");
        _stop.Cancel();
        _tray.Visible = false;
        _tray.Dispose();
        _microphone?.Dispose();
        _voice?.Dispose();
        _speaker?.Dispose();
        _detector?.Dispose();
        base.ExitThreadCore();
    }
}
