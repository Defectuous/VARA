using System.Text.Json;
using Vara.Listener.Audio;
using Vara.Listener.WakeWord;

namespace Vara.Listener.Tests;

/// <summary>
/// The C# wake-word pipeline must reproduce openWakeWord's Python output. The fixtures were produced by
/// openWakeWord itself (tools/wakeword/make_parity_fixtures.py) from the same audio and models.
/// </summary>
public class WakeWordParityTests
{
    private static readonly string Fixtures = Path.Combine(AppContext.BaseDirectory, "Fixtures");
    private static readonly string Models = Path.Combine(AppContext.BaseDirectory, "models");

    public static TheoryData<string> Clips => new() { "spoken-question", "synthetic-vara", "noise" };

    private static short[] LoadClip(string name)
    {
        var (samples, rate) = Wav.Decode(File.ReadAllBytes(Path.Combine(Fixtures, name + ".wav")));
        Assert.Equal(WakeWordDetector.SampleRate, rate);
        return samples;
    }

    private static string? WakeModel()
    {
        var path = Path.Combine(Models, "vara.onnx");
        return File.Exists(path) ? path : null;
    }

    private static WakeWordDetector NewDetector(string? wakeModel = null) =>
        new(Path.Combine(Models, "melspectrogram.onnx"), Path.Combine(Models, "embedding_model.onnx"), wakeModel);

    [Theory]
    [MemberData(nameof(Clips))]
    public void Features_match_openWakeWord(string clip)
    {
        var samples = LoadClip(clip);
        var expected = JsonSerializer.Deserialize<float[][]>(File.ReadAllText(Path.Combine(Fixtures, clip + ".features.json")))!;
        using var detector = NewDetector();

        var worst = 0f;
        for (var chunk = 0; chunk < expected.Length; chunk++)
        {
            var actual = detector.AddChunk(samples.AsSpan(chunk * WakeWordDetector.ChunkSamples, WakeWordDetector.ChunkSamples));
            Assert.Equal(expected[chunk].Length, actual.Length);
            for (var j = 0; j < actual.Length; j++)
            {
                worst = Math.Max(worst, Math.Abs(actual[j] - expected[chunk][j]));
            }
        }

        // Same models and arithmetic, so differences are only float rounding (values are ~0..30).
        Assert.True(worst < 1e-3f, $"Largest feature difference from openWakeWord: {worst}");
    }

    [Theory]
    [MemberData(nameof(Clips))]
    public void Scores_match_openWakeWord_once_the_buffers_are_full(string clip)
    {
        var model = WakeModel();
        var scoresFile = Path.Combine(Fixtures, clip + ".scores.json");
        if (model is null || !File.Exists(scoresFile))
        {
            return; // Recorded once the wake-word model has been trained; features are still checked above.
        }
        var samples = LoadClip(clip);
        var fixture = JsonSerializer.Deserialize<ScoresFixture>(File.ReadAllText(scoresFile))!;
        var modelHash = Convert.ToHexStringLower(System.Security.Cryptography.SHA256.HashData(File.ReadAllBytes(model)));
        Assert.True(fixture.model_sha256 == modelHash,
            "models/vara.onnx has changed since the score fixtures were made; regenerate them with " +
            "tools/wakeword/make_parity_fixtures.py.");
        var expected = fixture.scores;
        using var detector = NewDetector(model);

        for (var chunk = 0; chunk < expected.Length; chunk++)
        {
            var score = detector.Process(samples.AsSpan(chunk * WakeWordDetector.ChunkSamples, WakeWordDetector.ChunkSamples));
            if (chunk < detector.FeatureFrames - 1) // after chunk N-1 it holds N real features
            {
                // openWakeWord scores these against random-noise history; this port waits for real audio.
                Assert.Equal(0f, score);
                continue;
            }
            Assert.True(Math.Abs(score - expected[chunk]) < 1e-3f,
                $"Chunk {chunk}: C# score {score}, openWakeWord {expected[chunk]}");
        }
    }

    private sealed record ScoresFixture(string model_sha256, float[] scores);

    [Fact]
    public void Chunks_must_be_80_ms()
    {
        using var detector = NewDetector();

        Assert.Throws<ArgumentException>(() => detector.AddChunk(new short[1000]));
    }

    [Fact]
    public void Wav_round_trips()
    {
        var samples = new short[] { 0, 1, -1, short.MaxValue, short.MinValue, 1234 };

        var (decoded, rate) = Wav.Decode(Wav.Encode(samples));

        Assert.Equal(16_000, rate);
        Assert.Equal(samples, decoded);
    }
}
