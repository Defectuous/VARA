using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Vara.Listener.Audio;

namespace Vara.Listener.Backend;

public sealed record Transcript(
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("wake_word")] bool WakeWord,
    [property: JsonPropertyName("request")] string Request);

public sealed record ChatResult(int ConversationId, string Provider, string? Model);

/// <summary>An error from the backend, with a message fit to show (or say) to the user.</summary>
public sealed class BackendException(string message, HttpStatusCode? status = null, Exception? inner = null)
    : Exception(message, inner)
{
    public HttpStatusCode? Status { get; } = status;
}

/// <summary>The V.A.R.A. backend API, as used by the listener. The backend does all the thinking.</summary>
public sealed class VaraClient(HttpClient http) : IBackend
{
    public static VaraClient Create(string baseUrl) =>
        new(new HttpClient { BaseAddress = new Uri(baseUrl.TrimEnd('/') + "/"), Timeout = TimeSpan.FromMinutes(3) });

    public async Task<bool> IsUpAsync(CancellationToken ct = default)
    {
        try
        {
            using var response = await http.GetAsync("v1/health", ct);
            return response.IsSuccessStatusCode;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return false;
        }
    }

    public async Task<Transcript> TranscribeAsync(ReadOnlyMemory<short> samples, CancellationToken ct = default)
    {
        using var content = new ByteArrayContent(Wav.Encode(samples.Span));
        content.Headers.ContentType = new MediaTypeHeaderValue("audio/wav");
        using var response = await Send(() => http.PostAsync("v1/voice/transcribe", content, ct));
        await EnsureSuccess(response, ct);
        return (await response.Content.ReadFromJsonAsync<Transcript>(ct))!;
    }

    /// <summary>V.A.R.A.'s voice for one sentence, as WAV (null voice = the backend's default).</summary>
    public async Task<byte[]> SpeakAsync(string text, string? voice, CancellationToken ct = default)
    {
        var body = new Dictionary<string, object?> { ["text"] = text, ["voice"] = voice };
        using var response = await Send(() => http.PostAsync("v1/voice/speak", JsonContent.Create(body), ct));
        await EnsureSuccess(response, ct);
        return await response.Content.ReadAsByteArrayAsync(ct);
    }

    /// <summary>Ask V.A.R.A.; <paramref name="onText"/> receives the reply as it streams in, and
    /// <paramref name="onNotice"/> any "this is going to take a minute" lines that come before a slow reply.</summary>
    public async Task<ChatResult> ChatAsync(string text, int? conversationId, Action<string> onText, Action<string>? onNotice = null, CancellationToken ct = default)
    {
        var body = new Dictionary<string, object?>
        {
            ["messages"] = new[] { new { role = "user", content = text } },
            ["conversation_id"] = conversationId,
            ["spoken"] = true,
            // This device's clock, so V.A.R.A. knows the time where you are, not where the server is.
            ["client_time"] = DateTimeOffset.Now.ToString("yyyy-MM-dd'T'HH:mm:sszzz"),
            ["client_timezone"] = LocalTimeZoneName(),
        };
        using var request = new HttpRequestMessage(HttpMethod.Post, "v1/chat/stream") { Content = JsonContent.Create(body) };
        using var response = await Send(() => http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct));
        await EnsureSuccess(response, ct);

        await using var stream = await response.Content.ReadAsStreamAsync(ct);
        ChatResult? result = null;
        await foreach (var (name, data) in ServerSentEvents.ReadAsync(stream, ct))
        {
            using var json = JsonDocument.Parse(data);
            var root = json.RootElement;
            switch (name)
            {
                case "delta":
                    onText(root.GetProperty("text").GetString() ?? "");
                    break;
                case "notice":
                    onNotice?.Invoke(root.GetProperty("text").GetString() ?? "");
                    break;
                case "done":
                    result = new ChatResult(
                        root.GetProperty("conversation_id").GetInt32(),
                        root.GetProperty("provider").GetString() ?? "",
                        root.TryGetProperty("model", out var model) ? model.GetString() : null);
                    break;
                case "error":
                    throw new BackendException(root.GetProperty("message").GetString() ?? "The model failed.");
            }
        }
        return result ?? throw new BackendException("The reply stopped before it finished.");
    }

    /// <summary>The IANA name (e.g. America/New_York) that phones and browsers also use, else Windows' name.</summary>
    internal static string LocalTimeZoneName()
    {
        var local = TimeZoneInfo.Local;
        return TimeZoneInfo.TryConvertWindowsIdToIanaId(local.Id, out var iana) ? iana : local.Id;
    }

    private static async Task<HttpResponseMessage> Send(Func<Task<HttpResponseMessage>> send)
    {
        try
        {
            return await send();
        }
        catch (HttpRequestException ex)
        {
            throw new BackendException("I can't reach the V.A.R.A. server. Is it running?", inner: ex);
        }
    }

    private static async Task EnsureSuccess(HttpResponseMessage response, CancellationToken ct)
    {
        if (response.IsSuccessStatusCode)
        {
            return;
        }
        var detail = await response.Content.ReadAsStringAsync(ct);
        throw new BackendException(Describe(response.StatusCode, detail), response.StatusCode);
    }

    /// <summary>Turn the backend's error into something short enough to say out loud.</summary>
    internal static string Describe(HttpStatusCode status, string body)
    {
        string? detail = null;
        try
        {
            using var json = JsonDocument.Parse(body);
            if (json.RootElement.TryGetProperty("detail", out var d))
            {
                detail = d.ValueKind switch
                {
                    JsonValueKind.String => d.GetString(),
                    JsonValueKind.Object when d.TryGetProperty("message", out var m) => m.GetString(),
                    _ => null,
                };
            }
        }
        catch (JsonException)
        {
        }
        return status switch
        {
            HttpStatusCode.Forbidden => "That would need a cloud model, and the privacy mode is local only.",
            HttpStatusCode.Conflict when detail?.Contains("confirm_cloud") == true =>
                "That would need a cloud model. Please confirm it in the chat window.",
            HttpStatusCode.ServiceUnavailable => "No model could answer. Is Ollama running?",
            _ => detail ?? $"The V.A.R.A. server returned an error ({(int)status}).",
        };
    }
}

/// <summary>Minimal reader for the backend's server-sent events (<c>event:</c> and <c>data:</c> lines).</summary>
public static class ServerSentEvents
{
    public static async IAsyncEnumerable<(string Event, string Data)> ReadAsync(
        Stream stream, [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken ct = default)
    {
        using var reader = new StreamReader(stream, Encoding.UTF8);
        string name = "message";
        var data = new StringBuilder();
        while (await reader.ReadLineAsync(ct) is { } line)
        {
            if (line.Length == 0)
            {
                if (data.Length > 0)
                {
                    yield return (name, data.ToString());
                }
                name = "message";
                data.Clear();
            }
            else if (line.StartsWith("event:", StringComparison.Ordinal))
            {
                name = line[6..].Trim();
            }
            else if (line.StartsWith("data:", StringComparison.Ordinal))
            {
                if (data.Length > 0)
                {
                    data.Append('\n');
                }
                data.Append(line[5..].TrimStart());
            }
        }
        if (data.Length > 0)
        {
            yield return (name, data.ToString());
        }
    }
}
