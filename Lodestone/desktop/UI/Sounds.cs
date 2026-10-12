using System.Diagnostics;
using System.Runtime.InteropServices;
using Lodestone.Desktop.Core;

namespace Lodestone.Desktop.UI;

/// <summary>
/// Small 8-bit sound effects, synthesised when first needed - there are no
/// audio files. On Windows they play through winmm's PlaySound from memory;
/// elsewhere through afplay / paplay / aplay. One sound plays at a time, so a
/// quieter sound never cuts off a more important one that is still playing.
/// </summary>
public static class Sounds
{
    /// <summary>Off for --shots.</summary>
    public static bool Enabled { get; set; } = true;

    /// <summary>Raised when sound is switched on or off, so every mute button can follow.</summary>
    public static event Action? Changed;

    static Prefs? _prefs;

    public static void Init(Prefs p) => _prefs = p;

    public static bool On => _prefs?.SoundOn ?? true;

    public static void SetOn(bool on)
    {
        if (_prefs == null) return;
        _prefs.SoundOn = on;
        _prefs.Save();
        Changed?.Invoke();
        if (on) Play("success", true);
    }

    /// <summary>name → category (for the Settings switches) and priority (who may interrupt whom).</summary>
    static readonly Dictionary<string, (string cat, int prio, int gap)> Meta = new()
    {
        ["click"] = ("ui", 0, 40), ["tab"] = ("ui", 0, 40), ["toggle"] = ("ui", 0, 40), ["pop"] = ("ui", 1, 80),
        ["success"] = ("notify", 2, 150), ["error"] = ("notify", 2, 150), ["info"] = ("notify", 1, 150),
        ["start"] = ("server", 4, 500), ["stop"] = ("server", 4, 500), ["alert"] = ("server", 3, 4000),
        ["join"] = ("players", 3, 300), ["leave"] = ("players", 3, 300),
        ["chat"] = ("chat", 1, 900),
        ["intro"] = ("intro", 5, 1000),
    };

    public static readonly (string key, string label)[] Previews =
    {
        ("click", "Click"), ("tab", "Tab"), ("pop", "Dialog"), ("success", "Success"), ("error", "Error"),
        ("start", "Server start"), ("stop", "Server stop"), ("alert", "Alert"), ("join", "Player joins"),
        ("leave", "Player leaves"), ("chat", "Chat"), ("intro", "Intro"),
    };

    static bool CategoryOn(string cat) => _prefs == null || cat switch
    {
        "ui" => _prefs.SoundUi,
        "notify" => _prefs.SoundNotify,
        "server" => _prefs.SoundServer,
        "players" => _prefs.SoundPlayers,
        "chat" => _prefs.SoundChat,
        _ => true,
    };

    static readonly object Lock = new();
    static long _busyUntil;
    static int _busyPrio;
    static readonly Dictionary<string, long> _lastAt = new();

    /// <summary>Play a sound by name. force skips the switches and priorities (Settings previews).</summary>
    public static void Play(string name, bool force = false)
    {
        if (!Enabled || !Meta.TryGetValue(name, out var m)) return;
        if (!force && (!On || !CategoryOn(m.cat))) return;
        try
        {
            lock (Lock)
            {
                var now = Environment.TickCount64;
                if (!force)
                {
                    if (now < _busyUntil && m.prio < _busyPrio) return;
                    if (_lastAt.TryGetValue(name, out var last) && now - last < m.gap) return;
                }
                var clip = Get(name);
                _lastAt[name] = now;
                _busyUntil = now + clip.Ms;
                _busyPrio = m.prio;
                Output(name, clip);
            }
        }
        catch (Exception)
        {
            // a sound is never worth an error
        }
    }

    // ================================================================ clips

    sealed class Clip
    {
        public byte[] Wav = Array.Empty<byte>();
        public IntPtr Native;
        public int Ms;
        public string? File;
    }

    /// <summary>Peak level of each sound at full volume: clicks stay soft, events stand out.</summary>
    static readonly Dictionary<string, float> Loudness = new()
    {
        ["click"] = 0.42f, ["tab"] = 0.45f, ["toggle"] = 0.45f, ["pop"] = 0.55f, ["chat"] = 0.5f,
        ["info"] = 0.6f, ["success"] = 0.72f, ["error"] = 0.72f, ["alert"] = 0.75f,
        ["join"] = 0.75f, ["leave"] = 0.7f, ["start"] = 0.8f, ["stop"] = 0.8f, ["intro"] = 0.85f,
    };

    static readonly Dictionary<string, Clip> Clips = new();
    static int _builtVolume = -1;

    static int Volume => Math.Clamp(_prefs?.SoundVolume ?? 60, 0, 100);

    static Clip Get(string name)
    {
        if (_builtVolume != Volume)
        {
            // volume is baked into the samples: drop the old clips
            if (OperatingSystem.IsWindows()) PlaySound(IntPtr.Zero, IntPtr.Zero, 0);
            foreach (var c in Clips.Values)
                if (c.Native != IntPtr.Zero) Marshal.FreeHGlobal(c.Native);
            Clips.Clear();
            _builtVolume = Volume;
        }
        if (Clips.TryGetValue(name, out var clip)) return clip;
        var samples = Render(Define(name));
        // every sound is brought to its own loudness, then the volume slider applies
        var peak = samples.Max(Math.Abs);
        if (peak > 0)
        {
            var k = (Loudness.TryGetValue(name, out var l) ? l : 0.7f) / peak;
            for (var i = 0; i < samples.Length; i++) samples[i] *= k;
        }
        clip = new Clip { Wav = Wav(samples, Volume / 100.0), Ms = samples.Length * 1000 / Rate };
        Clips[name] = clip;
        return clip;
    }

    // ================================================================ output

    const uint SND_ASYNC = 0x0001, SND_NODEFAULT = 0x0002, SND_MEMORY = 0x0004;

    [DllImport("winmm.dll")]
    static extern bool PlaySound(IntPtr sound, IntPtr module, uint flags);

    static void Output(string name, Clip clip)
    {
        if (OperatingSystem.IsWindows())
        {
            // PlaySound reads the buffer while it plays, so it lives in unmanaged
            // memory that the garbage collector cannot move.
            if (clip.Native == IntPtr.Zero)
            {
                clip.Native = Marshal.AllocHGlobal(clip.Wav.Length);
                Marshal.Copy(clip.Wav, 0, clip.Native, clip.Wav.Length);
            }
            PlaySound(clip.Native, IntPtr.Zero, SND_ASYNC | SND_MEMORY | SND_NODEFAULT);
            return;
        }
        if (clip.File == null)
        {
            clip.File = Path.Combine(Path.GetTempPath(), "lodestone-" + name + "-" + _builtVolume + ".wav");
            System.IO.File.WriteAllBytes(clip.File, clip.Wav);
        }
        foreach (var player in OperatingSystem.IsMacOS() ? new[] { "afplay" } : new[] { "paplay", "aplay" })
        {
            try
            {
                var psi = new ProcessStartInfo(player) { UseShellExecute = false, CreateNoWindow = true };
                psi.ArgumentList.Add(clip.File);
                Process.Start(psi);
                return;
            }
            catch (Exception)
            {
                // try the next player
            }
        }
    }

    // ================================================================ synth

    const int Rate = 22050;

    enum W { Sine, Square, Square25, Triangle, Noise }

    readonly record struct Note(double At, double Freq, double Dur, W Wave, double Vol, double SlideTo = 0, double Decay = 4);

    // note frequencies
    const double C4 = 261.63, F3 = 174.61, A3 = 220.0, C5 = 523.25, E5 = 659.25, G5 = 783.99, A5 = 880.0,
                 C6 = 1046.5, E6 = 1318.51, G6 = 1567.98, C7 = 2093.0, E7 = 2637.02;

    static IEnumerable<Note> Define(string name)
    {
        switch (name)
        {
            case "click":
                yield return new Note(0, 2300, 16, W.Triangle, 0.22, 1500, 5);
                yield return new Note(0, 0, 7, W.Noise, 0.035, 0, 6);
                break;
            case "tab":
                yield return new Note(0, 1600, 26, W.Triangle, 0.24, 2200, 4);
                break;
            case "toggle":
                yield return new Note(0, 1100, 34, W.Square25, 0.10, 1700, 4);
                yield return new Note(0, 1100, 34, W.Triangle, 0.14, 1700, 4);
                break;
            case "pop":
                yield return new Note(0, 480, 95, W.Sine, 0.38, 940, 5);
                break;
            case "success":
                yield return new Note(0, E6, 85, W.Square25, 0.10, 0, 3);
                yield return new Note(0, E5, 85, W.Triangle, 0.20, 0, 3);
                yield return new Note(80, G6, 190, W.Square25, 0.10, 0, 4);
                yield return new Note(80, G5, 190, W.Triangle, 0.20, 0, 4);
                break;
            case "error":
                yield return new Note(0, A3, 120, W.Square, 0.11, 0, 1.5);
                yield return new Note(0, A3 / 2, 120, W.Triangle, 0.22, 0, 1.5);
                yield return new Note(130, F3, 220, W.Square, 0.11, 0, 2.5);
                yield return new Note(130, F3 / 2, 220, W.Triangle, 0.22, 0, 2.5);
                break;
            case "info":
                yield return new Note(0, C6, 120, W.Triangle, 0.28, 0, 4);
                yield return new Note(0, C7, 70, W.Sine, 0.07, 0, 6);
                break;
            case "start":
            {
                var notes = new[] { C5, E5, G5, C6 };
                for (var i = 0; i < notes.Length; i++)
                {
                    var last = i == notes.Length - 1;
                    yield return new Note(i * 70, notes[i], last ? 300 : 95, W.Square25, 0.09, 0, last ? 3 : 2);
                    yield return new Note(i * 70, notes[i], last ? 300 : 95, W.Triangle, 0.18, 0, last ? 3 : 2);
                }
                break;
            }
            case "stop":
            {
                var notes = new[] { C6, G5, E5, C5 };
                for (var i = 0; i < notes.Length; i++)
                {
                    var last = i == notes.Length - 1;
                    yield return new Note(i * 75, notes[i], last ? 320 : 95, W.Square25, 0.08, 0, last ? 3 : 2);
                    yield return new Note(i * 75, notes[i], last ? 320 : 95, W.Triangle, 0.18, 0, last ? 3 : 2);
                }
                break;
            }
            case "alert":
                for (var i = 0; i < 3; i++)
                {
                    yield return new Note(i * 120, A5, 75, W.Square, 0.10, 0, 1.5);
                    yield return new Note(i * 120, A5 / 2, 75, W.Triangle, 0.16, 0, 1.5);
                }
                break;
            case "join":
                yield return new Note(0, G6, 480, W.Sine, 0.30, 0, 5);
                yield return new Note(0, G6 * 2, 260, W.Sine, 0.06, 0, 8);
                yield return new Note(120, C7, 560, W.Sine, 0.28, 0, 5);
                yield return new Note(120, C7 * 2, 280, W.Sine, 0.05, 0, 8);
                break;
            case "leave":
                yield return new Note(0, C6, 380, W.Sine, 0.25, 0, 5);
                yield return new Note(130, G5, 460, W.Sine, 0.24, 0, 5);
                break;
            case "chat":
                yield return new Note(0, 1350, 50, W.Sine, 0.20, 1750, 5);
                break;
            case "intro":
            {
                var run = new[] { C5, G5, C6, E6, G6 };
                for (var i = 0; i < run.Length; i++)
                {
                    yield return new Note(i * 90, run[i], 150, W.Square25, 0.07, 0, 2.5);
                    yield return new Note(i * 90, run[i], 150, W.Triangle, 0.15, 0, 2.5);
                }
                foreach (var f in new[] { C6, E6, G6 })
                    yield return new Note(470, f, 950, W.Triangle, 0.11, 0, 3.2);
                yield return new Note(470, C4, 950, W.Triangle, 0.20, 0, 2.8);
                yield return new Note(520, C7, 380, W.Sine, 0.05, 0, 6);
                yield return new Note(640, E7, 380, W.Sine, 0.04, 0, 6);
                break;
            }
        }
    }

    static float[] Render(IEnumerable<Note> notes)
    {
        var list = notes.ToList();
        if (list.Count == 0) return new float[1];
        var totalMs = list.Max(n => n.At + n.Dur) + 25;
        var buf = new float[(int)(totalMs * Rate / 1000)];
        var rnd = new Random(7);
        foreach (var n in list)
        {
            var start = (int)(n.At * Rate / 1000);
            var len = (int)(n.Dur * Rate / 1000);
            double phase = 0;
            for (var i = 0; i < len && start + i < buf.Length; i++)
            {
                var p = i / (double)len;
                var f = n.SlideTo > 0 ? n.Freq + (n.SlideTo - n.Freq) * p : n.Freq;
                phase += f / Rate;
                phase -= Math.Floor(phase);
                var s = n.Wave switch
                {
                    W.Sine => Math.Sin(2 * Math.PI * phase),
                    W.Square => phase < 0.5 ? 1.0 : -1.0,
                    W.Square25 => phase < 0.25 ? 1.0 : -1.0,
                    W.Triangle => 4 * Math.Abs(phase - 0.5) - 1,
                    _ => rnd.NextDouble() * 2 - 1,
                };
                var attack = Math.Min(1, i / (Rate * 0.004));      // 4 ms in, no click
                var release = Math.Min(1, (len - i) / (Rate * 0.008)); // 8 ms out
                var env = attack * release * Math.Exp(-n.Decay * p);
                buf[start + i] += (float)(s * n.Vol * env);
            }
        }
        // one gentle low-pass takes the fizz off the square waves
        float y = 0;
        for (var i = 0; i < buf.Length; i++)
        {
            y += 0.55f * (buf[i] - y);
            buf[i] = y;
        }
        return buf;
    }

    static byte[] Wav(float[] samples, double volume)
    {
        var gain = volume;
        using var ms = new MemoryStream();
        using var w = new BinaryWriter(ms);
        var dataLen = samples.Length * 2;
        w.Write("RIFF"u8.ToArray());
        w.Write(36 + dataLen);
        w.Write("WAVE"u8.ToArray());
        w.Write("fmt "u8.ToArray());
        w.Write(16);
        w.Write((short)1);       // PCM
        w.Write((short)1);       // mono
        w.Write(Rate);
        w.Write(Rate * 2);       // bytes per second
        w.Write((short)2);       // block align
        w.Write((short)16);      // bits
        w.Write("data"u8.ToArray());
        w.Write(dataLen);
        foreach (var s in samples)
            w.Write((short)Math.Clamp(s * gain * 32767, -32767, 32767));
        w.Flush();
        return ms.ToArray();
    }
}
