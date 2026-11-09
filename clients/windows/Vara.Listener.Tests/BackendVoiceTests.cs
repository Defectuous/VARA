using System.Collections.Concurrent;
using System.Text;
using Vara.Listener.Voice;

namespace Vara.Listener.Tests;

public class BackendVoiceTests
{
    private sealed class RecordingPlayer : IAudioPlayer
    {
        public ConcurrentQueue<string> Played { get; } = new();
        public TimeSpan Duration { get; init; } = TimeSpan.Zero;

        public async Task PlayAsync(byte[] wav, CancellationToken ct)
        {
            await Task.Delay(Duration, ct);
            Played.Enqueue(Encoding.UTF8.GetString(wav));
        }
    }

    private sealed class FallbackVoice : IVoiceOutput
    {
        public ConcurrentQueue<string> Said { get; } = new();
        public void Say(string sentence) => Said.Enqueue(sentence);
        public void Stop() { }
        public Task WaitUntilIdleAsync(CancellationToken ct = default) => Task.CompletedTask;
    }

    [Fact]
    public async Task Sentences_play_in_order_even_if_later_ones_synthesize_first()
    {
        // The first sentence takes longest to synthesize; it must still be spoken first.
        var delays = new Dictionary<string, int> { ["One."] = 150, ["Two."] = 10, ["Three."] = 50 };
        var player = new RecordingPlayer();
        using var voice = new BackendVoice(async (text, ct) =>
        {
            await Task.Delay(delays[text], ct);
            return Encoding.UTF8.GetBytes(text);
        }, player, new FallbackVoice());

        voice.Say("One.");
        voice.Say("Two.");
        voice.Say("Three.");
        await voice.WaitUntilIdleAsync().WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Equal(new[] { "One.", "Two.", "Three." }, player.Played);
    }

    [Fact]
    public async Task A_sentence_the_backend_cannot_voice_uses_the_windows_voice()
    {
        var player = new RecordingPlayer();
        var fallback = new FallbackVoice();
        using var voice = new BackendVoice((text, _) => text == "Two."
            ? Task.FromException<byte[]>(new HttpRequestException("offline"))
            : Task.FromResult(Encoding.UTF8.GetBytes(text)), player, fallback);

        voice.Say("One.");
        voice.Say("Two.");
        voice.Say("Three.");
        await voice.WaitUntilIdleAsync().WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Equal(new[] { "One.", "Three." }, player.Played);
        Assert.Equal(new[] { "Two." }, fallback.Said);
    }

    [Fact]
    public async Task Stop_drops_everything_queued()
    {
        var player = new RecordingPlayer { Duration = TimeSpan.FromMilliseconds(200) };
        using var voice = new BackendVoice((text, _) => Task.FromResult(Encoding.UTF8.GetBytes(text)), player, new FallbackVoice());

        voice.Say("One.");
        voice.Say("Two.");
        voice.Say("Three.");
        await Task.Delay(50);
        voice.Stop();
        await voice.WaitUntilIdleAsync().WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Empty(player.Played); // "One." was cut off mid-way, the rest never started
    }

    [Fact]
    public async Task Idle_when_nothing_is_queued()
    {
        using var voice = new BackendVoice((t, _) => Task.FromResult(Array.Empty<byte>()), new RecordingPlayer(), new FallbackVoice());

        await voice.WaitUntilIdleAsync().WaitAsync(TimeSpan.FromSeconds(1));
    }
}
