using System.Net;
using System.Threading.Channels;
using Vara.Listener.Audio;
using Vara.Listener.Backend;
using Vara.Listener.Voice;

namespace Vara.Listener;

public enum ListenerState
{
    Listening,
    Paused,
    Capturing,
    Thinking,
    Speaking,
}

public interface IWakeWord
{
    float Process(ReadOnlySpan<short> chunk);
    void Reset();
}

public interface IBackend
{
    Task<Transcript> TranscribeAsync(ReadOnlyMemory<short> samples, CancellationToken ct = default);
    /// <summary><paramref name="onNotice"/> receives "this is going to take a minute" lines while a slow reply hasn't started.</summary>
    Task<ChatResult> ChatAsync(string text, int? conversationId, Action<string> onText, Action<string>? onNotice = null, CancellationToken ct = default);
}

public interface IVoiceOutput
{
    void Say(string sentence);
    void Stop();
    Task WaitUntilIdleAsync(CancellationToken ct = default);
}

public interface IChimes
{
    void Listening();
    void Thinking();
    void Error();
}

/// <summary>
/// The listener's state machine. Audio arrives in 80 ms chunks:
///   Listening  - the wake-word model scores each chunk; at the threshold it starts capturing,
///                keeping the audio from just before so the transcript includes "VARA".
///   Capturing  - records until you stop talking (<see cref="Endpointer"/>).
///   Thinking   - the backend transcribes the clip. If the transcript doesn't start with VARA it was a
///                false wake-up and is dropped; otherwise the request goes to chat.
///   Speaking   - the reply is spoken sentence by sentence as it streams. Saying "VARA" interrupts it.
/// Saying only "VARA" gets a "Yes?" and one follow-up capture that doesn't need the wake word.
/// </summary>
public sealed class VoiceLoop
{
    private readonly ListenerSettings _settings;
    private readonly IWakeWord _wake;
    private readonly IBackend _backend;
    private readonly IVoiceOutput _voice;
    private readonly IChimes _chimes;
    private readonly object _gate = new();
    private readonly Queue<short[]> _preRoll = new();
    private readonly int _preRollChunks;
    private readonly NoiseFloor _noise = new();

    private ListenerState _state = ListenerState.Listening;
    private bool _paused;
    private List<short[]> _capture = new();
    private Endpointer? _endpointer;
    private bool _followUp;
    private int _turn; // increases with each capture, so a stale request can't change the state
    private CancellationTokenSource _replyCts = new();
    private int? _conversationId;
    private DateTimeOffset _lastExchange = DateTimeOffset.MinValue;

    public event Action<ListenerState>? StateChanged;

    /// <summary>Something the user should know about (shown as a tray notification).</summary>
    public event Action<string>? Problem;

    public ListenerState State
    {
        get { lock (_gate) return _state; }
    }

    public int FalseWakeUps { get; private set; }

    /// <summary>The background work for the current request, if any (lets tests wait for it).</summary>
    internal Task CurrentReply { get; private set; } = Task.CompletedTask;

    public VoiceLoop(ListenerSettings settings, IWakeWord wake, IBackend backend, IVoiceOutput voice, IChimes chimes)
    {
        _settings = settings;
        _wake = wake;
        _backend = backend;
        _voice = voice;
        _chimes = chimes;
        _preRollChunks = Math.Max(1, (int)Math.Round(settings.PreRollSeconds / Endpointer.ChunkSeconds));
    }

    public bool Paused
    {
        get { lock (_gate) return _paused; }
        set
        {
            lock (_gate)
            {
                _paused = value;
                if (value)
                {
                    CancelReply();
                    _voice.Stop();
                    SetState(ListenerState.Paused);
                }
                else if (_state == ListenerState.Paused)
                {
                    _wake.Reset();
                    SetState(ListenerState.Listening);
                }
            }
        }
    }

    public async Task RunAsync(ChannelReader<short[]> chunks, CancellationToken ct)
    {
        await foreach (var chunk in chunks.ReadAllAsync(ct))
        {
            try
            {
                Process(chunk);
            }
            catch (Exception ex)
            {
                Log.Error("Processing audio", ex);
            }
        }
    }

    /// <summary>Handle one 80 ms chunk of microphone audio.</summary>
    internal void Process(short[] chunk)
    {
        lock (_gate)
        {
            _preRoll.Enqueue(chunk);
            while (_preRoll.Count > _preRollChunks)
            {
                _preRoll.Dequeue();
            }

            switch (_state)
            {
                case ListenerState.Listening:
                    _noise.Update(chunk);
                    if (_wake.Process(chunk) >= _settings.Threshold)
                    {
                        Log.Info("Wake word heard");
                        BeginCapture(followUp: false);
                    }
                    break;

                case ListenerState.Speaking:
                    if (_wake.Process(chunk) >= _settings.ThresholdWhileSpeaking)
                    {
                        Log.Info("Wake word heard while speaking; stopping the reply");
                        CancelReply();
                        _voice.Stop();
                        BeginCapture(followUp: false);
                    }
                    break;

                case ListenerState.Capturing:
                    _capture.Add(chunk);
                    if (_endpointer!.Feed(chunk))
                    {
                        EndCapture();
                    }
                    break;
            }
        }
    }

    private void BeginCapture(bool followUp)
    {
        _chimes.Listening();
        _turn++;
        _followUp = followUp;
        // The pre-roll holds "VARA" itself; a follow-up starts fresh, after the "Yes?".
        _capture = followUp ? new List<short[]>() : new List<short[]>(_preRoll);
        // After "Yes?" people take a moment to answer, so a follow-up waits longer for them to start.
        _endpointer = new Endpointer(_noise.Db, _settings.EndSilenceSeconds, _settings.MaxRequestSeconds,
            startTimeoutSeconds: followUp ? 4.0 : 2.5, ignoreSeconds: followUp ? 0 : 0.4);
        SetState(ListenerState.Capturing);
    }

    private void EndCapture()
    {
        Log.Info($"Capture ended: {_capture.Count * Endpointer.ChunkSeconds:0.0} s" +
                 (_endpointer!.HeardSpeechAfterWake ? "" : " (no speech after the wake word)") +
                 (_followUp ? ", follow-up" : "") +
                 $" (peak {_endpointer.PeakDb:0} dB, speech level {_endpointer.ThresholdDb:0} dB)");
        _chimes.Thinking();
        SetState(ListenerState.Thinking);
        var samples = _capture.SelectMany(c => c).ToArray();
        var turn = _turn;
        var followUp = _followUp;
        _replyCts = new CancellationTokenSource();
        var ct = _replyCts.Token;
        CurrentReply = Task.Run(() => HandleRequestAsync(samples, followUp, turn, ct));
    }

    private async Task HandleRequestAsync(short[] samples, bool followUp, int turn, CancellationToken ct)
    {
        try
        {
            var transcript = await _backend.TranscribeAsync(samples, ct);
            string request;
            if (followUp)
            {
                request = transcript.Text.Trim();
            }
            else if (!transcript.WakeWord)
            {
                FalseWakeUps++;
                Log.Info($"False wake-up ignored (transcript did not start with VARA; {FalseWakeUps} so far)");
                ReturnToListening(turn);
                return;
            }
            else
            {
                request = transcript.Request.Trim();
            }

            if (request.Length == 0)
            {
                if (followUp)
                {
                    Log.Info("Nothing heard after \"Yes?\"; listening for VARA again");
                    ReturnToListening(turn);
                    return;
                }
                Log.Info("Only the wake word was heard; asking \"Yes?\"");
                StartSpeaking(turn);
                _voice.Say("Yes?");
                await _voice.WaitUntilIdleAsync(ct);
                lock (_gate)
                {
                    if (turn == _turn && !_paused)
                    {
                        BeginCapture(followUp: true);
                    }
                }
                return;
            }

            Log.Info($"Request heard ({request.Length} characters); asking V.A.R.A.");
            if (DateTimeOffset.Now - _lastExchange > TimeSpan.FromMinutes(_settings.ConversationIdleMinutes))
            {
                _conversationId = null; // a new conversation after a quiet spell
            }
            StartSpeaking(turn);
            var splitter = new SentenceSplitter();
            void Speak(string text)
            {
                foreach (var sentence in splitter.Add(text))
                {
                    _voice.Say(sentence);
                }
            }
            ChatResult result;
            try
            {
                result = await _backend.ChatAsync(request, _conversationId, Speak, _voice.Say, ct);
            }
            catch (BackendException ex) when (ex.Status == HttpStatusCode.NotFound && _conversationId is not null)
            {
                // The conversation was deleted (e.g. in the chat page): start a new one and ask again.
                Log.Info($"Conversation {_conversationId} no longer exists; starting a new one");
                _conversationId = null;
                result = await _backend.ChatAsync(request, null, Speak, _voice.Say, ct);
            }
            if (splitter.Flush() is { } rest)
            {
                _voice.Say(rest);
            }
            _conversationId = result.ConversationId;
            _lastExchange = DateTimeOffset.Now;
            Log.Info($"Answered by {result.Provider} {result.Model} (conversation {result.ConversationId})");
            await _voice.WaitUntilIdleAsync(ct);
            ReturnToListening(turn);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // Interrupted by "VARA" or by pausing; the new state is already set.
        }
        catch (BackendException ex)
        {
            Log.Warn($"Backend: {ex.Message}");
            await ReportAsync(ex.Message, turn, ct);
        }
        catch (Exception ex)
        {
            Log.Error("Handling a request", ex);
            await ReportAsync("Something went wrong. The details are in the listener log.", turn, ct);
        }
    }

    private async Task ReportAsync(string message, int turn, CancellationToken ct)
    {
        _chimes.Error();
        Problem?.Invoke(message);
        StartSpeaking(turn);
        _voice.Say(message);
        try
        {
            await _voice.WaitUntilIdleAsync(ct);
        }
        catch (OperationCanceledException)
        {
            return;
        }
        ReturnToListening(turn);
    }

    private void StartSpeaking(int turn)
    {
        lock (_gate)
        {
            if (turn != _turn || _paused)
            {
                return;
            }
            // Detection was skipped while capturing and thinking; start it fresh for interruptions.
            _wake.Reset();
            SetState(ListenerState.Speaking);
        }
    }

    private void ReturnToListening(int turn)
    {
        lock (_gate)
        {
            if (turn != _turn || _paused)
            {
                return;
            }
            _wake.Reset(); // so the "VARA" just handled can't trigger again
            SetState(ListenerState.Listening);
            Log.Info("Listening for VARA");
        }
    }

    private void CancelReply()
    {
        _turn++;
        _replyCts.Cancel();
    }

    private void SetState(ListenerState state)
    {
        if (_state == state)
        {
            return;
        }
        _state = state;
        StateChanged?.Invoke(state);
    }
}

/// <summary>The real chimes, behind the interface the voice loop uses.</summary>
public sealed class Chimes : IChimes
{
    public void Listening() => Chime.Listening();
    public void Thinking() => Chime.Thinking();
    public void Error() => Chime.Error();
}
