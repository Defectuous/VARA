using System.Runtime.InteropServices;
using System.Threading.Channels;
using NAudio.Wave;
using Vara.Listener.WakeWord;

namespace Vara.Listener.Audio;

/// <summary>
/// Records 16 kHz mono 16-bit audio from a Windows microphone and delivers it as 80 ms chunks
/// (1,280 samples), the unit the wake-word model works in.
/// </summary>
public sealed class Microphone : IDisposable
{
    private readonly WaveIn _waveIn;
    private readonly Channel<short[]> _chunks = Channel.CreateBounded<short[]>(
        new BoundedChannelOptions(250) { FullMode = BoundedChannelFullMode.DropOldest, SingleReader = true });
    private readonly short[] _pending = new short[WakeWordDetector.ChunkSamples];
    private int _pendingCount;

    /// <summary>Raised if recording stops on its own, e.g. the microphone was unplugged or access was denied.</summary>
    public event Action<Exception?>? Stopped;

    public ChannelReader<short[]> Chunks => _chunks.Reader;

    public Microphone(int deviceNumber)
    {
        _waveIn = new WaveIn
        {
            DeviceNumber = deviceNumber,
            WaveFormat = new WaveFormat(WakeWordDetector.SampleRate, 16, 1),
            BufferMilliseconds = 40,
            NumberOfBuffers = 4,
        };
        _waveIn.DataAvailable += OnData;
        _waveIn.RecordingStopped += (_, e) => Stopped?.Invoke(e.Exception);
    }

    /// <summary>Names of the recording devices, by device number.</summary>
    public static IReadOnlyList<string> DeviceNames() =>
        Enumerable.Range(0, WaveIn.DeviceCount).Select(i => WaveIn.GetCapabilities(i).ProductName).ToList();

    /// <summary>
    /// The name of the microphone Windows currently has as its default recording device (what device -1
    /// opens), or null if Windows doesn't say.
    /// </summary>
    public static string? DefaultDeviceName()
    {
        const int waveMapper = -1;
        const int drvmMapperPreferredGet = 0x2015;
        if (waveInMessage(new IntPtr(waveMapper), drvmMapperPreferredGet, out var device, out _) != 0)
        {
            return null;
        }
        var names = DeviceNames();
        return device >= 0 && device < names.Count ? names[device] : null;
    }

    [DllImport("winmm.dll")]
    private static extern int waveInMessage(IntPtr deviceId, int message, out int param1, out int param2);

    public void Start() => _waveIn.StartRecording();

    private void OnData(object? sender, WaveInEventArgs e)
    {
        for (var i = 0; i + 1 < e.BytesRecorded; i += 2)
        {
            _pending[_pendingCount++] = (short)(e.Buffer[i] | (e.Buffer[i + 1] << 8));
            if (_pendingCount == _pending.Length)
            {
                _chunks.Writer.TryWrite((short[])_pending.Clone());
                _pendingCount = 0;
            }
        }
    }

    public void Dispose()
    {
        try
        {
            _waveIn.StopRecording();
        }
        catch (Exception ex)
        {
            Log.Warn($"Stopping the microphone: {ex.Message}");
        }
        _waveIn.Dispose();
        _chunks.Writer.TryComplete();
    }
}
