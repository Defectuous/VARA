using System.Text.Json;

namespace Vara.Listener;

/// <summary>Settings from appsettings.json beside the executable. Every value has a working default.</summary>
public sealed record ListenerSettings
{
    /// <summary>The V.A.R.A. backend. Only this PC by default.</summary>
    public string BackendUrl { get; init; } = "http://127.0.0.1:8000";

    /// <summary>The trained wake-word model, relative to the executable.</summary>
    public string WakeWordModel { get; init; } = "models/vara.onnx";

    /// <summary>Score (0..1) at which "VARA" counts as heard.</summary>
    public float Threshold { get; init; } = 0.5f;

    /// <summary>A higher bar while V.A.R.A. is talking, so its own voice is less likely to trigger it.</summary>
    public float ThresholdWhileSpeaking { get; init; } = 0.8f;

    /// <summary>Windows recording device number; -1 is the default microphone.</summary>
    public int MicrophoneDevice { get; init; } = -1;

    /// <summary>
    /// V.A.R.A.'s voice: a Kokoro voice from the backend (the SiteRecorder/SiteReader voices), e.g. af_heart,
    /// af_bella, bf_emma. Null uses the backend's default (VARA_TTS_VOICE, af_heart).
    /// </summary>
    public string? Voice { get; init; }

    /// <summary>Windows voice used if the backend can't speak (Zira: female, US English).</summary>
    public string? FallbackVoice { get; init; } = "Microsoft Zira Desktop";

    /// <summary>Audio kept from before the wake word, so the transcript includes "VARA" itself.</summary>
    public double PreRollSeconds { get; init; } = 1.5;

    /// <summary>
    /// Silence that ends a spoken request. Long enough for a pause to think mid-sentence (0.9 s cut people
    /// off); each extra 0.1 s adds the same to every reply's wait.
    /// </summary>
    public double EndSilenceSeconds { get; init; } = 1.8;

    /// <summary>Longest spoken request.</summary>
    public double MaxRequestSeconds { get; init; } = 30;

    /// <summary>Start a new conversation after this long without speaking to V.A.R.A.</summary>
    public double ConversationIdleMinutes { get; init; } = 10;

    public static ListenerSettings Load(string path)
    {
        if (!File.Exists(path))
        {
            return new ListenerSettings();
        }
        var options = new JsonSerializerOptions { PropertyNameCaseInsensitive = true, ReadCommentHandling = JsonCommentHandling.Skip };
        return JsonSerializer.Deserialize<ListenerSettings>(File.ReadAllText(path), options) ?? new ListenerSettings();
    }
}
