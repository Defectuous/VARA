using System.Speech.Synthesis;

namespace Vara.Listener.Voice;

/// <summary>Speaks replies with a Windows voice. Sentences queue up; <see cref="Stop"/> cuts them off.</summary>
public sealed class Speaker : IVoiceOutput, IDisposable
{
    private readonly SpeechSynthesizer _synth = new();
    private int _pending;
    private TaskCompletionSource _idle = NewIdle(completed: true);

    public bool IsSpeaking => Volatile.Read(ref _pending) > 0;

    public Speaker(string? voice)
    {
        _synth.SetOutputToDefaultAudioDevice();
        if (!string.IsNullOrWhiteSpace(voice))
        {
            try
            {
                _synth.SelectVoice(voice);
            }
            catch (ArgumentException)
            {
                Log.Warn($"Voice \"{voice}\" isn't installed; using the default voice.");
            }
        }
        _synth.SpeakCompleted += (_, _) =>
        {
            if (Interlocked.Decrement(ref _pending) <= 0)
            {
                Volatile.Write(ref _pending, 0);
                _idle.TrySetResult();
            }
        };
    }

    public static IReadOnlyList<string> InstalledVoices()
    {
        using var synth = new SpeechSynthesizer();
        return synth.GetInstalledVoices().Where(v => v.Enabled).Select(v => v.VoiceInfo.Name).ToList();
    }

    public void Say(string sentence)
    {
        if (Interlocked.Increment(ref _pending) == 1)
        {
            _idle = NewIdle(completed: false);
        }
        _synth.SpeakAsync(sentence);
    }

    /// <summary>Stop talking now, e.g. when you say "VARA" over the reply.</summary>
    public void Stop()
    {
        _synth.SpeakAsyncCancelAll();
    }

    /// <summary>Completes when everything queued has been spoken (or stopped).</summary>
    public Task WaitUntilIdleAsync(CancellationToken cancellationToken = default) =>
        IsSpeaking ? _idle.Task.WaitAsync(cancellationToken) : Task.CompletedTask;

    private static TaskCompletionSource NewIdle(bool completed)
    {
        var tcs = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        if (completed)
        {
            tcs.SetResult();
        }
        return tcs;
    }

    public void Dispose()
    {
        _synth.SpeakAsyncCancelAll();
        _synth.Dispose();
    }
}
