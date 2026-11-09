using Microsoft.ML.OnnxRuntime;
using Microsoft.ML.OnnxRuntime.Tensors;

namespace Vara.Listener.WakeWord;

/// <summary>
/// Streaming openWakeWord detector: a C# port of openWakeWord's <c>AudioFeatures</c> streaming path
/// (openwakeword/utils.py) followed by the trained wake-word classifier.
///
/// For every 80 ms chunk (1,280 samples of 16 kHz, 16-bit audio):
///   1. the newest 1,760 samples (the chunk plus 480 samples of context) go through the mel-spectrogram
///      model, giving 8 frames of 32 bins, each transformed as x/10 + 2;
///   2. the last 76 mel frames go through Google's speech-embedding model, giving one 96-value feature;
///   3. the last N features (16 for our model) go through the wake-word model, giving a 0..1 score.
///
/// The mel buffer starts as 76 rows of ones, as in openWakeWord. openWakeWord also pre-fills its feature
/// buffer with features of random noise so it can score immediately; this port instead returns 0 until
/// it has N real features (1.28 s). From then on its output matches openWakeWord's, which the parity
/// tests check against features and scores produced by the Python code.
/// </summary>
public sealed class WakeWordDetector : IWakeWord, IDisposable
{
    public const int SampleRate = 16_000;
    public const int ChunkSamples = 1_280;
    private const int MelContextSamples = 160 * 3;
    private const int MelInputSamples = ChunkSamples + MelContextSamples;
    private const int MelBins = 32;
    private const int EmbeddingWindow = 76;
    private const int EmbeddingSize = 96;

    private readonly InferenceSession _melspec;
    private readonly InferenceSession _embedding;
    private readonly InferenceSession? _classifier;
    private readonly string _melspecInput;
    private readonly string _embeddingInput;
    private readonly string? _classifierInput;

    private readonly short[] _raw = new short[MelInputSamples];
    private int _rawCount;
    private readonly LinkedList<float[]> _mel = new();
    private readonly LinkedList<float[]> _features = new();

    /// <summary>How many embedding frames the classifier looks at (16 = 1.28 s).</summary>
    public int FeatureFrames { get; }

    public WakeWordDetector(string melspecModel, string embeddingModel, string? wakeWordModel)
    {
        // One thread each, like openWakeWord's defaults: the models are tiny and this keeps CPU use low.
        var options = new SessionOptions { IntraOpNumThreads = 1, InterOpNumThreads = 1 };
        _melspec = new InferenceSession(melspecModel, options);
        _embedding = new InferenceSession(embeddingModel, options);
        _melspecInput = _melspec.InputMetadata.Keys.First();
        _embeddingInput = _embedding.InputMetadata.Keys.First();
        if (wakeWordModel is not null)
        {
            _classifier = new InferenceSession(wakeWordModel, options);
            _classifierInput = _classifier.InputMetadata.Keys.First();
            FeatureFrames = _classifier.InputMetadata[_classifierInput].Dimensions[1];
        }
        else
        {
            FeatureFrames = 16;
        }
        Reset();
    }

    /// <summary>Clear all audio history, e.g. after a detection so the same "VARA" doesn't fire twice.</summary>
    public void Reset()
    {
        _rawCount = 0;
        _mel.Clear();
        for (var i = 0; i < EmbeddingWindow; i++)
        {
            _mel.AddLast(Enumerable.Repeat(1f, MelBins).ToArray());
        }
        _features.Clear();
    }

    /// <summary>Feed one 80 ms chunk; returns the wake-word score (0 until enough audio has been heard).</summary>
    public float Process(ReadOnlySpan<short> chunk)
    {
        if (_classifier is null)
        {
            throw new InvalidOperationException("No wake-word model was loaded.");
        }
        AddChunk(chunk);
        if (_features.Count < FeatureFrames)
        {
            return 0f;
        }

        var input = new DenseTensor<float>(new[] { 1, FeatureFrames, EmbeddingSize });
        var row = 0;
        foreach (var feature in _features)
        {
            for (var j = 0; j < EmbeddingSize; j++)
            {
                input[0, row, j] = feature[j];
            }
            row++;
        }
        using var results = _classifier.Run(new[] { NamedOnnxValue.CreateFromTensor(_classifierInput!, input) });
        return results.First().AsEnumerable<float>().First();
    }

    /// <summary>Feed one chunk and return the newest 96-value feature (used by the parity tests).</summary>
    internal float[] AddChunk(ReadOnlySpan<short> chunk)
    {
        if (chunk.Length != ChunkSamples)
        {
            throw new ArgumentException($"Chunks must be exactly {ChunkSamples} samples.", nameof(chunk));
        }

        // Keep the newest 1,760 samples (openWakeWord: raw_data_buffer[-n_samples - 480:]).
        var keep = Math.Min(_rawCount, MelContextSamples);
        Array.Copy(_raw, _rawCount - keep, _raw, 0, keep);
        chunk.CopyTo(_raw.AsSpan(keep));
        _rawCount = keep + ChunkSamples;

        // Mel-spectrogram of the raw int16 values as float (not normalised), then x/10 + 2.
        var samples = new DenseTensor<float>(new[] { 1, _rawCount });
        for (var i = 0; i < _rawCount; i++)
        {
            samples[0, i] = _raw[i];
        }
        using (var melOut = _melspec.Run(new[] { NamedOnnxValue.CreateFromTensor(_melspecInput, samples) }))
        {
            var values = melOut.First().AsEnumerable<float>().ToArray();
            for (var offset = 0; offset + MelBins <= values.Length; offset += MelBins)
            {
                var frame = new float[MelBins];
                for (var j = 0; j < MelBins; j++)
                {
                    frame[j] = values[offset + j] / 10f + 2f;
                }
                _mel.AddLast(frame);
            }
        }
        while (_mel.Count > EmbeddingWindow)
        {
            _mel.RemoveFirst();
        }

        // Embedding of the last 76 mel frames, shaped [1, 76, 32, 1].
        var window = new DenseTensor<float>(new[] { 1, EmbeddingWindow, MelBins, 1 });
        var f = 0;
        foreach (var frame in _mel)
        {
            for (var j = 0; j < MelBins; j++)
            {
                window[0, f, j, 0] = frame[j];
            }
            f++;
        }
        float[] feature;
        using (var embOut = _embedding.Run(new[] { NamedOnnxValue.CreateFromTensor(_embeddingInput, window) }))
        {
            feature = embOut.First().AsEnumerable<float>().ToArray();
        }
        _features.AddLast(feature);
        while (_features.Count > FeatureFrames)
        {
            _features.RemoveFirst();
        }
        return feature;
    }

    public void Dispose()
    {
        _melspec.Dispose();
        _embedding.Dispose();
        _classifier?.Dispose();
    }
}
