using System.Text;
using System.Text.RegularExpressions;

namespace Vara.Listener.Voice;

/// <summary>Turns a streamed reply into sentences that can be spoken as soon as each one is complete.</summary>
public sealed partial class SentenceSplitter
{
    private readonly StringBuilder _buffer = new();
    private bool _inCode;

    /// <summary>Add streamed text; returns any sentences that are now complete.</summary>
    public IEnumerable<string> Add(string text)
    {
        _buffer.Append(text);
        var sentences = new List<string>();
        while (TryTakeSentence(out var sentence))
        {
            sentences.Add(sentence);
        }
        return sentences;
    }

    /// <summary>Whatever is left once the reply has finished.</summary>
    public string? Flush()
    {
        var rest = _buffer.ToString();
        _buffer.Clear();
        return Speakable(rest);
    }

    private bool TryTakeSentence(out string sentence)
    {
        sentence = "";
        var text = _buffer.ToString();
        for (var i = 0; i < text.Length; i++)
        {
            if (text.AsSpan(i).StartsWith("```"))
            {
                _inCode = !_inCode;
                if (!_inCode)
                {
                    // A whole code block: say where to find it instead of reading it out.
                    _buffer.Remove(0, i + 3);
                    sentence = "I've put the code in the chat window.";
                    return true;
                }
                i += 2;
                continue;
            }
            if (_inCode)
            {
                continue;
            }
            var end = text[i] == '\n' || ((text[i] is '.' or '!' or '?') && i + 1 < text.Length && char.IsWhiteSpace(text[i + 1]));
            if (!end)
            {
                continue;
            }
            var candidate = Speakable(text[..(i + 1)]);
            _buffer.Remove(0, i + 1);
            if (candidate is null)
            {
                return TryTakeSentence(out sentence);
            }
            sentence = candidate;
            return true;
        }
        return false;
    }

    /// <summary>Strip Markdown so the voice doesn't read out asterisks and hashes. Null if nothing is left.</summary>
    public static string? Speakable(string text)
    {
        var cleaned = Links().Replace(text, "$1");
        cleaned = Symbols().Replace(cleaned, "");
        cleaned = ListMarker().Replace(cleaned, "");
        cleaned = Spaces().Replace(cleaned, " ").Trim();
        return cleaned.Any(char.IsLetterOrDigit) ? cleaned : null;
    }

    [GeneratedRegex(@"\[([^\]]+)\]\([^)]+\)")]
    private static partial Regex Links();

    [GeneratedRegex(@"[*_`#>|~]")]
    private static partial Regex Symbols();

    [GeneratedRegex(@"^\s*(?:[-+]|\d+\.)\s+", RegexOptions.Multiline)]
    private static partial Regex ListMarker();

    [GeneratedRegex(@"\s+")]
    private static partial Regex Spaces();
}
