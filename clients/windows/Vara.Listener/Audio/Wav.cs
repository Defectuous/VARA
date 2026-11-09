using System.Buffers.Binary;

namespace Vara.Listener.Audio;

/// <summary>16-bit PCM mono WAV encoding and decoding: the format the backend's /v1/voice/transcribe takes.</summary>
public static class Wav
{
    public static byte[] Encode(ReadOnlySpan<short> samples, int sampleRate = 16_000)
    {
        var dataBytes = samples.Length * 2;
        var wav = new byte[44 + dataBytes];
        var span = wav.AsSpan();
        "RIFF"u8.CopyTo(span);
        BinaryPrimitives.WriteInt32LittleEndian(span[4..], 36 + dataBytes);
        "WAVE"u8.CopyTo(span[8..]);
        "fmt "u8.CopyTo(span[12..]);
        BinaryPrimitives.WriteInt32LittleEndian(span[16..], 16); // PCM header size
        BinaryPrimitives.WriteInt16LittleEndian(span[20..], 1); // PCM
        BinaryPrimitives.WriteInt16LittleEndian(span[22..], 1); // mono
        BinaryPrimitives.WriteInt32LittleEndian(span[24..], sampleRate);
        BinaryPrimitives.WriteInt32LittleEndian(span[28..], sampleRate * 2); // byte rate
        BinaryPrimitives.WriteInt16LittleEndian(span[32..], 2); // block align
        BinaryPrimitives.WriteInt16LittleEndian(span[34..], 16); // bits per sample
        "data"u8.CopyTo(span[36..]);
        BinaryPrimitives.WriteInt32LittleEndian(span[40..], dataBytes);
        for (var i = 0; i < samples.Length; i++)
        {
            BinaryPrimitives.WriteInt16LittleEndian(span[(44 + i * 2)..], samples[i]);
        }
        return wav;
    }

    /// <summary>Reads a 16-bit PCM mono WAV file, returning its samples and sample rate.</summary>
    public static (short[] Samples, int SampleRate) Decode(ReadOnlySpan<byte> wav)
    {
        if (wav.Length < 12 || !wav[..4].SequenceEqual("RIFF"u8) || !wav[8..12].SequenceEqual("WAVE"u8))
        {
            throw new InvalidDataException("Not a WAV file.");
        }
        int? rate = null;
        var pos = 12;
        while (pos + 8 <= wav.Length)
        {
            var id = wav.Slice(pos, 4);
            var size = BinaryPrimitives.ReadInt32LittleEndian(wav[(pos + 4)..]);
            var body = wav.Slice(pos + 8, Math.Min(size, wav.Length - pos - 8));
            if (id.SequenceEqual("fmt "u8))
            {
                var format = BinaryPrimitives.ReadInt16LittleEndian(body);
                var channels = BinaryPrimitives.ReadInt16LittleEndian(body[2..]);
                var bits = BinaryPrimitives.ReadInt16LittleEndian(body[14..]);
                if (format != 1 || channels != 1 || bits != 16)
                {
                    throw new InvalidDataException("Only 16-bit PCM mono WAV is supported.");
                }
                rate = BinaryPrimitives.ReadInt32LittleEndian(body[4..]);
            }
            else if (id.SequenceEqual("data"u8))
            {
                if (rate is null)
                {
                    throw new InvalidDataException("WAV data comes before its format.");
                }
                var samples = new short[body.Length / 2];
                for (var i = 0; i < samples.Length; i++)
                {
                    samples[i] = BinaryPrimitives.ReadInt16LittleEndian(body[(i * 2)..]);
                }
                return (samples, rate.Value);
            }
            pos += 8 + size + (size & 1);
        }
        throw new InvalidDataException("WAV file has no audio data.");
    }
}
