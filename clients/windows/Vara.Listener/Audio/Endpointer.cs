namespace Vara.Listener.Audio;

/// <summary>
/// Decides when a spoken request has ended. It always waits at least <c>startTimeout</c> seconds for you to
/// start talking (people often pause after "VARA", and the end of "VARA" itself must not count as the
/// request); after that the request ends once there have been <c>endSilence</c> seconds below the speech
/// level, or at <c>maxSeconds</c>. The first <c>ignoreSeconds</c> are skipped: they hold the tail of the
/// wake word, which the detector fires on before you have finished saying it. The speech level is set a margin above the
/// room's background noise, which <see cref="NoiseFloor"/> tracks while V.A.R.A. is waiting for its name.
/// </summary>
public sealed class Endpointer
{
    public const double ChunkSeconds = 0.08;
    private const double MarginDb = 6;
    private const double MinThresholdDb = -55;

    private readonly double _thresholdDb;
    private readonly int _startTimeoutChunks;
    private readonly int _endSilenceChunks;
    private readonly int _maxChunks;
    private readonly int _ignoreChunks;
    private int _chunks;
    private int _lastSpeechChunk;

    public bool HeardSpeechAfterWake { get; private set; }

    /// <summary>The speech level in dBFS: anything at or above it counts as talking.</summary>
    public double ThresholdDb => _thresholdDb;

    /// <summary>The loudest chunk so far, in dBFS (for the log).</summary>
    public double PeakDb { get; private set; } = -96;

    public Endpointer(double noiseFloorDb, double endSilenceSeconds, double maxSeconds, double startTimeoutSeconds = 2.5,
        double ignoreSeconds = 0)
    {
        _thresholdDb = Math.Max(noiseFloorDb + MarginDb, MinThresholdDb);
        // Whole chunks, so 0.8 s is exactly 10 chunks rather than a floating-point near miss.
        _startTimeoutChunks = ToChunks(startTimeoutSeconds);
        _endSilenceChunks = ToChunks(endSilenceSeconds);
        _maxChunks = ToChunks(maxSeconds);
        _ignoreChunks = ignoreSeconds > 0 ? ToChunks(ignoreSeconds) : 0;
    }

    private static int ToChunks(double seconds) => Math.Max(1, (int)Math.Round(seconds / ChunkSeconds));

    /// <summary>Feed the next 80 ms chunk; returns true when the request is over.</summary>
    public bool Feed(ReadOnlySpan<short> chunk)
    {
        _chunks++;
        var level = LevelDb(chunk);
        PeakDb = Math.Max(PeakDb, level);
        if (_chunks > _ignoreChunks && level >= _thresholdDb)
        {
            _lastSpeechChunk = _chunks;
            HeardSpeechAfterWake = true;
        }
        if (_chunks >= _maxChunks)
        {
            return true;
        }
        if (_chunks < _startTimeoutChunks)
        {
            return false;
        }
        return !HeardSpeechAfterWake || _chunks - _lastSpeechChunk >= _endSilenceChunks;
    }

    /// <summary>RMS level in dBFS (0 = full scale; digital silence is -96).</summary>
    public static double LevelDb(ReadOnlySpan<short> samples)
    {
        if (samples.IsEmpty)
        {
            return -96;
        }
        double sum = 0;
        foreach (var s in samples)
        {
            sum += (double)s * s;
        }
        var rms = Math.Sqrt(sum / samples.Length) / 32768.0;
        return Math.Max(20 * Math.Log10(rms + 1e-9), -96);
    }
}

/// <summary>Tracks background noise: follows quiet moments quickly and loud ones slowly.</summary>
public sealed class NoiseFloor
{
    public double Db { get; private set; } = -60;

    public void Update(ReadOnlySpan<short> chunk)
    {
        var level = Endpointer.LevelDb(chunk);
        Db = level < Db ? Db + 0.3 * (level - Db) : Db + 0.01 * (level - Db);
    }
}
