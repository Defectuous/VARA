using System.Collections.Concurrent;
using Vara.Listener.Backend;

namespace Vara.Listener.Tests;

public class VoiceLoopTests
{
    private const int Chunk = 1280;
    private static readonly short[] Silence = new short[Chunk];
    private static readonly short[] Speech = Enumerable.Range(0, Chunk).Select(i => (short)(i % 2 == 0 ? 6000 : -6000)).ToArray();

    private sealed class FakeWake : IWakeWord
    {
        public Queue<float> Scores { get; } = new();
        public int Resets { get; private set; }
        public float Process(ReadOnlySpan<short> chunk) => Scores.Count > 0 ? Scores.Dequeue() : 0f;
        public void Reset() => Resets++;
    }

    private sealed class FakeBackend(string transcript, bool wakeWord, string request, params string[] reply) : IBackend
    {
        public List<int> TranscribedSampleCounts { get; } = new();
        public List<(string Text, int? Conversation)> Asked { get; } = new();
        public Exception? Fail { get; set; }

        public Task<Transcript> TranscribeAsync(ReadOnlyMemory<short> samples, CancellationToken ct = default)
        {
            TranscribedSampleCounts.Add(samples.Length);
            if (Fail is not null)
            {
                throw Fail;
            }
            return Task.FromResult(new Transcript(transcript, wakeWord, request));
        }

        public HashSet<int> DeletedConversations { get; } = new();

        public List<string> Notices { get; } = new();

        public Task<ChatResult> ChatAsync(string text, int? conversationId, Action<string> onText, Action<string>? onNotice = null, CancellationToken ct = default)
        {
            Asked.Add((text, conversationId));
            foreach (var notice in Notices)
            {
                onNotice?.Invoke(notice);
            }
            if (conversationId is { } id && DeletedConversations.Contains(id))
            {
                throw new BackendException("Conversation not found", System.Net.HttpStatusCode.NotFound);
            }
            foreach (var part in reply)
            {
                onText(part);
            }
            return Task.FromResult(new ChatResult(42, "ollama", "llama3.1:8b"));
        }
    }

    private sealed class FakeVoice : IVoiceOutput
    {
        public ConcurrentQueue<string> Said { get; } = new();
        public int Stops { get; private set; }
        public void Say(string sentence) => Said.Enqueue(sentence);
        public void Stop() => Stops++;
        public Task WaitUntilIdleAsync(CancellationToken ct = default) => Task.CompletedTask;
    }

    private sealed class FakeChimes : IChimes
    {
        public List<string> Played { get; } = new();
        public void Listening() => Played.Add("listening");
        public void Thinking() => Played.Add("thinking");
        public void Error() => Played.Add("error");
    }

    private static readonly ListenerSettings Settings = new() { PreRollSeconds = 0.4, EndSilenceSeconds = 0.4, MaxRequestSeconds = 5 };

    private static (VoiceLoop Loop, FakeWake Wake, FakeVoice Voice, FakeChimes Chimes) Make(FakeBackend backend)
    {
        var wake = new FakeWake();
        var voice = new FakeVoice();
        var chimes = new FakeChimes();
        return (new VoiceLoop(Settings, wake, backend, voice, chimes), wake, voice, chimes);
    }

    /// <summary>Background audio, "VARA" detected, a spoken request, then silence until the request ends.</summary>
    private static async Task SayVaraAndRequest(VoiceLoop loop, FakeWake wake, int speechChunks = 10)
    {
        for (var i = 0; i < 20; i++)
        {
            loop.Process(Silence);
        }
        wake.Scores.Enqueue(0.97f);
        loop.Process(Speech);
        Assert.Equal(ListenerState.Capturing, loop.State);
        for (var i = 0; i < speechChunks; i++)
        {
            loop.Process(Speech);
        }
        for (var i = 0; i < 40 && loop.State == ListenerState.Capturing; i++) // at least the 2.5 s start window
        {
            loop.Process(Silence);
        }
        await loop.CurrentReply;
    }

    [Fact]
    public async Task A_request_is_transcribed_answered_and_spoken()
    {
        var backend = new FakeBackend("VARA, what time is it?", true, "what time is it?", "It is ", "ten past four. ", "Anything else?");
        var (loop, wake, voice, chimes) = Make(backend);

        await SayVaraAndRequest(loop, wake);

        // Pre-roll (0.4 s = 5 chunks, including the "VARA" chunk) + the capture, which lasts at least the
        // 2.5 s start window (31 chunks: 10 speech, then silence).
        Assert.Equal((5 + 31) * Chunk, Assert.Single(backend.TranscribedSampleCounts));
        Assert.Equal(("what time is it?", (int?)null), Assert.Single(backend.Asked));
        Assert.Equal(new[] { "It is ten past four.", "Anything else?" }, voice.Said);
        Assert.Equal(new[] { "listening", "thinking" }, chimes.Played);
        Assert.Equal(ListenerState.Listening, loop.State);
        Assert.True(wake.Resets >= 1); // so the same "VARA" can't trigger again
    }

    [Fact]
    public async Task A_slow_reply_is_announced_before_it_is_spoken()
    {
        var backend = new FakeBackend("VARA, plan my week", true, "plan my week", "Monday: ", "rest.");
        backend.Notices.Add("This is going to take a minute.");
        backend.Notices.Add("Still working on it.");
        var (loop, wake, voice, _) = Make(backend);

        await SayVaraAndRequest(loop, wake);

        Assert.Equal(new[] { "This is going to take a minute.", "Still working on it.", "Monday: rest." }, voice.Said);
    }

    [Fact]
    public async Task The_conversation_continues_on_the_next_request()
    {
        var backend = new FakeBackend("VARA, and tomorrow?", true, "and tomorrow?", "Sunny.");
        var (loop, wake, _, _) = Make(backend);

        await SayVaraAndRequest(loop, wake);
        await SayVaraAndRequest(loop, wake);

        Assert.Equal(new int?[] { null, 42 }, backend.Asked.Select(a => a.Conversation));
    }

    [Fact]
    public async Task A_deleted_conversation_is_replaced_by_a_new_one_without_an_error()
    {
        var backend = new FakeBackend("VARA, hello", true, "hello", "Hi.");
        var (loop, wake, voice, chimes) = Make(backend);
        string? problem = null;
        loop.Problem += m => problem = m;

        await SayVaraAndRequest(loop, wake); // starts conversation 42
        backend.DeletedConversations.Add(42); // deleted in the chat page
        await SayVaraAndRequest(loop, wake);

        Assert.Equal(new int?[] { null, 42, null }, backend.Asked.Select(a => a.Conversation));
        Assert.Null(problem);
        Assert.DoesNotContain("error", chimes.Played);
        Assert.Equal(new[] { "Hi.", "Hi." }, voice.Said);
    }

    [Fact]
    public async Task A_false_wake_up_is_dropped_silently()
    {
        var backend = new FakeBackend("Sarah, pass the salt", false, "Sarah, pass the salt");
        var (loop, wake, voice, _) = Make(backend);

        await SayVaraAndRequest(loop, wake);

        Assert.Empty(backend.Asked);
        Assert.Empty(voice.Said);
        Assert.Equal(1, loop.FalseWakeUps);
        Assert.Equal(ListenerState.Listening, loop.State);
    }

    [Fact]
    public async Task Just_the_name_gets_a_yes_and_a_follow_up_without_the_wake_word()
    {
        var backend = new FakeBackend("VARA.", true, "");
        var (loop, wake, voice, _) = Make(backend);

        await SayVaraAndRequest(loop, wake);

        Assert.Equal(new[] { "Yes?" }, voice.Said);
        Assert.Equal(ListenerState.Capturing, loop.State); // listening for the follow-up, no "VARA" needed
    }

    [Fact]
    public async Task Backend_errors_are_spoken_and_reported()
    {
        var backend = new FakeBackend("", true, "") { Fail = new BackendException("I can't reach the V.A.R.A. server. Is it running?") };
        var (loop, wake, voice, chimes) = Make(backend);
        string? problem = null;
        loop.Problem += m => problem = m;

        await SayVaraAndRequest(loop, wake);

        Assert.Equal("I can't reach the V.A.R.A. server. Is it running?", problem);
        Assert.Contains(problem, voice.Said);
        Assert.Contains("error", chimes.Played);
        Assert.Equal(ListenerState.Listening, loop.State);
    }

    [Fact]
    public void Pausing_stops_listening_and_resuming_starts_fresh()
    {
        var (loop, wake, voice, _) = Make(new FakeBackend("", true, ""));

        loop.Paused = true;
        wake.Scores.Enqueue(0.99f);
        loop.Process(Speech);

        Assert.Equal(ListenerState.Paused, loop.State);
        Assert.Single(wake.Scores); // the detector wasn't even asked
        var resetsBefore = wake.Resets;
        loop.Paused = false;
        Assert.Equal(ListenerState.Listening, loop.State);
        Assert.True(wake.Resets > resetsBefore);
    }

    [Fact]
    public void A_score_below_the_threshold_does_nothing()
    {
        var (loop, wake, _, chimes) = Make(new FakeBackend("", true, ""));

        wake.Scores.Enqueue(0.3f);
        loop.Process(Speech);

        Assert.Equal(ListenerState.Listening, loop.State);
        Assert.Empty(chimes.Played);
    }
}
