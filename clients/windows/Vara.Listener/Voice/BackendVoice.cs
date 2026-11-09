using System.Threading.Channels;
using NAudio.Wave;

namespace Vara.Listener.Voice;

/// <summary>Plays one WAV clip; completes when it has finished (or was stopped).</summary>
public interface IAudioPlayer
{
    Task PlayAsync(byte[] wav, CancellationToken ct);
}

/// <summary>
/// V.A.R.A.'s voice: sentences are spoken with the backend's Kokoro voice (POST /v1/voice/speak).
/// Each sentence's audio is requested as soon as it arrives, so the next one is ready while the current
/// one plays. If the backend can't produce speech, that sentence falls back to a Windows voice.
/// </summary>
public sealed class BackendVoice : IVoiceOutput, IDisposable
{
    private readonly Func<string, CancellationToken, Task<byte[]>> _synthesize;
    private readonly IAudioPlayer _player;
    private readonly IVoiceOutput _fallback;
    private readonly Channel<Item> _queue = Channel.CreateUnbounded<Item>(new UnboundedChannelOptions { SingleReader = true });
    private readonly object _gate = new();
    private readonly Task _worker;
    private CancellationTokenSource _stopCts = new();
    private int _generation;
    private int _pending;
    private TaskCompletionSource _idle = Completed();
    private bool _warnedFallback;

    private sealed record Item(int Generation, string Text, Task<byte[]?> Audio);

    public BackendVoice(Func<string, CancellationToken, Task<byte[]>> synthesize, IAudioPlayer player, IVoiceOutput fallback)
    {
        _synthesize = synthesize;
        _player = player;
        _fallback = fallback;
        _worker = Task.Run(PlayLoopAsync);
    }

    public void Say(string sentence)
    {
        lock (_gate)
        {
            if (_pending++ == 0)
            {
                _idle = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            }
            var ct = _stopCts.Token;
            _queue.Writer.TryWrite(new Item(_generation, sentence, SynthesizeOrNull(sentence, ct)));
        }
    }

    /// <summary>Stop talking now and drop anything queued.</summary>
    public void Stop()
    {
        lock (_gate)
        {
            _generation++;
            _stopCts.Cancel();
            _stopCts = new CancellationTokenSource();
        }
        _fallback.Stop();
    }

    public Task WaitUntilIdleAsync(CancellationToken ct = default)
    {
        lock (_gate)
        {
            return _pending == 0 ? Task.CompletedTask : _idle.Task.WaitAsync(ct);
        }
    }

    private async Task<byte[]?> SynthesizeOrNull(string text, CancellationToken ct)
    {
        try
        {
            return await _synthesize(text, ct);
        }
        catch (OperationCanceledException)
        {
            return null;
        }
        catch (Exception ex)
        {
            if (!_warnedFallback)
            {
                _warnedFallback = true;
                Log.Warn($"The backend voice isn't available ({ex.Message}); using the Windows voice");
            }
            return null;
        }
    }

    private async Task PlayLoopAsync()
    {
        await foreach (var item in _queue.Reader.ReadAllAsync())
        {
            try
            {
                CancellationToken ct;
                lock (_gate)
                {
                    if (item.Generation != _generation)
                    {
                        continue; // stopped before it was spoken
                    }
                    ct = _stopCts.Token;
                }
                var audio = await item.Audio;
                if (ct.IsCancellationRequested)
                {
                    continue;
                }
                if (audio is not null)
                {
                    await _player.PlayAsync(audio, ct);
                }
                else
                {
                    _fallback.Say(item.Text);
                    await _fallback.WaitUntilIdleAsync(ct);
                }
            }
            catch (OperationCanceledException)
            {
            }
            catch (Exception ex)
            {
                Log.Error("Playing speech", ex);
            }
            finally
            {
                lock (_gate)
                {
                    if (--_pending == 0)
                    {
                        _idle.TrySetResult();
                    }
                }
            }
        }
    }

    private static TaskCompletionSource Completed()
    {
        var tcs = new TaskCompletionSource();
        tcs.SetResult();
        return tcs;
    }

    public void Dispose()
    {
        Stop();
        _queue.Writer.TryComplete();
    }
}

/// <summary>Plays WAV audio on the default output device with NAudio.</summary>
public sealed class WavePlayer : IAudioPlayer
{
    public async Task PlayAsync(byte[] wav, CancellationToken ct)
    {
        using var reader = new WaveFileReader(new MemoryStream(wav));
        using var output = new WaveOut();
        var done = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        output.PlaybackStopped += (_, _) => done.TrySetResult();
        output.Init(reader);
        output.Play();
        using (ct.Register(() => output.Stop()))
        {
            await done.Task;
        }
        ct.ThrowIfCancellationRequested();
    }
}
