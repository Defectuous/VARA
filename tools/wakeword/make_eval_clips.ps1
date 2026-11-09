<#
  Wake-word evaluation clips spoken by the Windows voices (a different speech engine from the Piper voices
  the model was trained on), at 16 kHz mono 16-bit.
    positive\  should wake: "VARA" at the start, in several phrasings, voices and speaking rates
    negative\  should not wake: sound-alikes and everyday speech
#>
param([Parameter(Mandatory)][string]$OutDir)
Add-Type -AssemblyName System.Speech
$positive = @(
    "Vara, what time is it?",
    "Hey Vara, turn up the volume.",
    "Okay Vara, what's the weather like today?",
    "Vara.",
    "Vara, remind me to call my mother tonight."
)
$negative = @(
    "Sarah, what time is it?",
    "Farah called earlier this morning.",
    "Laura is very far away right now.",
    "The car is parked in the garage.",
    "I would rather have a cup of coffee.",
    "Hey Siri, what's up?",
    "Okay Google, play some music.",
    "Barbara went to the store.",
    "The Sahara desert is very hot.",
    "He drives a red Ferrari.",
    "She wore a sparkling tiara.",
    "There are various options available."
)
$voices = @("Microsoft David Desktop", "Microsoft Zira Desktop")
$rates = @(-3, 0, 3)
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
foreach ($set in @(@{ Name = "positive"; Phrases = $positive }, @{ Name = "negative"; Phrases = $negative })) {
    $dir = Join-Path $OutDir $set.Name
    New-Item -ItemType Directory -Force $dir | Out-Null
    $i = 0
    foreach ($voice in $voices) {
        foreach ($rate in $rates) {
            foreach ($phrase in $set.Phrases) {
                $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
                $s.SelectVoice($voice)
                $s.Rate = $rate
                $file = Join-Path $dir ("{0:D3}_{1}_r{2}.wav" -f $i, ($voice -split ' ')[1], $rate)
                $s.SetOutputToWaveFile($file, $format)
                $s.Speak($phrase)
                $s.Dispose()
                Add-Content (Join-Path $dir "index.tsv") ("{0}`t{1}" -f (Split-Path $file -Leaf), $phrase)
                $i++
            }
        }
    }
}
"positive: $((Get-ChildItem (Join-Path $OutDir 'positive') -Filter *.wav).Count)  negative: $((Get-ChildItem (Join-Path $OutDir 'negative') -Filter *.wav).Count)"
