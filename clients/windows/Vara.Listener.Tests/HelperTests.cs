using System.Net;
using System.Text;
using Vara.Listener.Audio;
using Vara.Listener.Backend;
using Vara.Listener.Voice;

namespace Vara.Listener.Tests;

public class EndpointerTests
{
    private static short[] Tone(double dbfs) =>
        Enumerable.Range(0, 1280).Select(i => (short)(Math.Sin(i * 0.3) * 32767 * Math.Pow(10, dbfs / 20) * Math.Sqrt(2))).ToArray();

    [Fact]
    public void Level_is_measured_in_dbfs()
    {
        Assert.InRange(Endpointer.LevelDb(Tone(-20)), -21, -19);
        Assert.Equal(-96, Endpointer.LevelDb(new short[1280]));
    }

    [Fact]
    public void A_request_ends_after_the_configured_silence()
    {
        var endpointer = new Endpointer(noiseFloorDb: -60, endSilenceSeconds: 0.4, maxSeconds: 10, startTimeoutSeconds: 0.8);

        for (var i = 0; i < 10; i++)
        {
            Assert.False(endpointer.Feed(Tone(-20)));
        }
        var silentChunks = 0;
        while (!endpointer.Feed(Tone(-70)))
        {
            silentChunks++;
        }

        Assert.True(endpointer.HeardSpeechAfterWake);
        Assert.Equal(4, silentChunks); // 0.4 s = 5 chunks; the 5th returns true
    }

    [Fact]
    public void Background_noise_below_the_margin_does_not_count_as_speech()
    {
        var endpointer = new Endpointer(noiseFloorDb: -40, endSilenceSeconds: 0.4, maxSeconds: 10, startTimeoutSeconds: 0.8);
        var chunks = 0;
        while (!endpointer.Feed(Tone(-37)))
        {
            chunks++;
        }

        Assert.False(endpointer.HeardSpeechAfterWake);
        Assert.Equal(9, chunks); // gave up after the 0.8 s start timeout
    }

    [Fact]
    public void A_pause_after_the_wake_word_does_not_end_the_request()
    {
        var endpointer = new Endpointer(noiseFloorDb: -60, endSilenceSeconds: 0.9, maxSeconds: 15, startTimeoutSeconds: 2.5);

        for (var i = 0; i < 19; i++) // 1.5 s of silence: "VARA ... "
        {
            Assert.False(endpointer.Feed(Tone(-70)));
        }
        for (var i = 0; i < 10; i++) // "... what time is it?"
        {
            Assert.False(endpointer.Feed(Tone(-25)));
        }

        Assert.True(endpointer.HeardSpeechAfterWake);
    }

    [Fact]
    public void The_end_of_the_wake_word_then_a_pause_does_not_end_the_request()
    {
        // What happened at 13:06: the tail of "VARA", a pause for the chime, then the question.
        var endpointer = new Endpointer(noiseFloorDb: -60, endSilenceSeconds: 0.9, maxSeconds: 15,
            startTimeoutSeconds: 2.5, ignoreSeconds: 0.4);

        for (var i = 0; i < 3; i++) // "...ra" (ignored)
        {
            Assert.False(endpointer.Feed(Tone(-25)));
        }
        Assert.False(endpointer.HeardSpeechAfterWake);
        for (var i = 0; i < 2; i++) // a little more of it, then a pause longer than the end silence
        {
            Assert.False(endpointer.Feed(Tone(-25)));
        }
        for (var i = 0; i < 13; i++)
        {
            Assert.False(endpointer.Feed(Tone(-70)));
        }
        for (var i = 0; i < 10; i++) // "what time is it?"
        {
            Assert.False(endpointer.Feed(Tone(-25)));
        }
        var silent = 0;
        while (!endpointer.Feed(Tone(-70)))
        {
            silent++;
        }
        Assert.Equal(10, silent); // ends 0.9 s after the question
        Assert.InRange(endpointer.PeakDb, -26, -24);
    }

    [Fact]
    public void A_short_request_still_ends_once_the_start_window_has_passed()
    {
        var endpointer = new Endpointer(noiseFloorDb: -60, endSilenceSeconds: 0.4, maxSeconds: 10, startTimeoutSeconds: 2.5);
        var chunks = 0;
        for (var i = 0; i < 5; i++, chunks++)
        {
            endpointer.Feed(Tone(-25));
        }
        while (!endpointer.Feed(Tone(-70)))
        {
            chunks++;
        }
        Assert.Equal(30, chunks); // the 31st chunk (2.5 s) ends it
    }

    [Fact]
    public void Quiet_speech_six_db_above_the_room_counts()
    {
        var endpointer = new Endpointer(noiseFloorDb: -50, endSilenceSeconds: 0.4, maxSeconds: 10);

        endpointer.Feed(Tone(-43.5));

        Assert.True(endpointer.HeardSpeechAfterWake);
    }

    [Fact]
    public void A_request_is_cut_off_at_the_maximum_length()
    {
        var endpointer = new Endpointer(noiseFloorDb: -60, endSilenceSeconds: 1, maxSeconds: 0.8);
        var chunks = 0;
        while (!endpointer.Feed(Tone(-20)))
        {
            chunks++;
        }

        Assert.Equal(9, chunks); // 10 chunks = 0.8 s
    }

    [Fact]
    public void The_noise_floor_follows_quiet_rooms_quickly_and_noise_slowly()
    {
        var floor = new NoiseFloor();
        for (var i = 0; i < 50; i++)
        {
            floor.Update(Tone(-70));
        }
        Assert.InRange(floor.Db, -71, -69);

        floor.Update(Tone(-20)); // one loud chunk (speech) barely moves it
        Assert.True(floor.Db < -68);
    }
}

public class SentenceSplitterTests
{
    [Fact]
    public void Sentences_are_released_as_soon_as_they_are_complete()
    {
        var splitter = new SentenceSplitter();

        Assert.Empty(splitter.Add("It is ten "));
        Assert.Equal(new[] { "It is ten past four." }, splitter.Add("past four. Anything"));
        Assert.Empty(splitter.Add(" else"));
        Assert.Equal("Anything else", splitter.Flush());
    }

    [Fact]
    public void Markdown_is_not_read_aloud()
    {
        var splitter = new SentenceSplitter();

        var sentences = splitter.Add("## Weather\n* **Sunny** today, see [the forecast](http://x).\n").ToList();

        Assert.Equal(new[] { "Weather", "Sunny today, see the forecast." }, sentences);
    }

    [Fact]
    public void Code_blocks_are_replaced_with_a_pointer_to_the_chat_window()
    {
        var splitter = new SentenceSplitter();

        var sentences = splitter.Add("Here you go:\n```python\nprint('hi')\n```\nDone. ").ToList();

        Assert.Equal(new[] { "Here you go:", "I've put the code in the chat window.", "Done." }, sentences);
    }

    [Fact]
    public void Decimal_numbers_do_not_split_sentences()
    {
        var splitter = new SentenceSplitter();

        Assert.Empty(splitter.Add("Pi is 3.14 roughly"));
        Assert.Equal("Pi is 3.14 roughly", splitter.Flush());
    }
}

public class BackendTests
{
    [Fact]
    public async Task Server_sent_events_are_parsed()
    {
        var text = "event: delta\ndata: {\"text\": \"Hi\"}\n\nevent: done\ndata: {\"conversation_id\": 3}\n\n";

        var events = new List<(string, string)>();
        await foreach (var e in ServerSentEvents.ReadAsync(new MemoryStream(Encoding.UTF8.GetBytes(text))))
        {
            events.Add(e);
        }

        Assert.Equal(new[] { ("delta", "{\"text\": \"Hi\"}"), ("done", "{\"conversation_id\": 3}") }, events);
    }

    [Theory]
    [InlineData(HttpStatusCode.Forbidden, "{\"detail\": \"blocked\"}", "privacy mode is local only")]
    [InlineData(HttpStatusCode.Conflict, "{\"detail\": \"cloud; resend with confirm_cloud=true\"}", "confirm it in the chat window")]
    [InlineData(HttpStatusCode.ServiceUnavailable, "{\"detail\": {\"message\": \"none\"}}", "Is Ollama running?")]
    [InlineData(HttpStatusCode.BadRequest, "{\"detail\": \"The audio is empty\"}", "The audio is empty")]
    [InlineData(HttpStatusCode.InternalServerError, "oops", "error (500)")]
    public void Errors_become_short_spoken_messages(HttpStatusCode status, string body, string expected)
    {
        Assert.Contains(expected, VaraClient.Describe(status, body));
    }
}

public class ClockTests
{
    [Fact]
    public void The_time_zone_is_sent_as_an_iana_name_when_windows_can_map_it()
    {
        var name = VaraClient.LocalTimeZoneName();

        Assert.False(string.IsNullOrWhiteSpace(name));
        if (TimeZoneInfo.TryConvertWindowsIdToIanaId(TimeZoneInfo.Local.Id, out var iana))
        {
            Assert.Equal(iana, name);
            Assert.Contains('/', name); // e.g. America/New_York
        }
    }
}
