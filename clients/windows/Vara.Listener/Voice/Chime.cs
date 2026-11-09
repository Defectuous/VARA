using NAudio.Wave;

namespace Vara.Listener.Voice;

/// <summary>Short generated tones, so V.A.R.A. needs no sound files.</summary>
public static class Chime
{
    /// <summary>Two rising notes: "I heard you, go ahead."</summary>
    public static void Listening() => Play((660, 0.07), (880, 0.09));

    /// <summary>One soft note: "Got it, working on it."</summary>
    public static void Thinking() => Play((520, 0.08));

    /// <summary>Two falling notes: something went wrong.</summary>
    public static void Error() => Play((440, 0.1), (300, 0.14));

    private static void Play(params (double Hz, double Seconds)[] notes)
    {
        const int rate = 22_050;
        var samples = new List<short>();
        foreach (var (hz, seconds) in notes)
        {
            var count = (int)(rate * seconds);
            for (var i = 0; i < count; i++)
            {
                // A short fade in and out avoids clicks.
                var envelope = Math.Min(1.0, Math.Min(i, count - i) / (rate * 0.01));
                samples.Add((short)(Math.Sin(2 * Math.PI * hz * i / rate) * envelope * 6000));
            }
            samples.AddRange(new short[rate / 50]); // 20 ms gap
        }
        var bytes = new byte[samples.Count * 2];
        Buffer.BlockCopy(samples.ToArray(), 0, bytes, 0, bytes.Length);

        var output = new WaveOut();
        var stream = new RawSourceWaveStream(new MemoryStream(bytes), new WaveFormat(rate, 16, 1));
        output.PlaybackStopped += (_, _) =>
        {
            output.Dispose();
            stream.Dispose();
        };
        try
        {
            output.Init(stream);
            output.Play();
        }
        catch (Exception ex)
        {
            Log.Warn($"Couldn't play a chime: {ex.Message}");
            output.Dispose();
            stream.Dispose();
        }
    }
}
