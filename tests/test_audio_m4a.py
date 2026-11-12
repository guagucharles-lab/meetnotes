import wave

import numpy as np


def test_wav_to_m4a_mixes_to_small_mono(tmp_path):
    import av
    from meetnotes.screen import wav_to_m4a
    rate, secs = 16000, 20
    t = np.arange(rate * secs) / rate
    left = (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16)
    right = (np.sin(2 * np.pi * 330 * t) * 8000).astype(np.int16)
    wav = tmp_path / "audio.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(np.column_stack([left, right]).tobytes())
    out = wav_to_m4a(wav, tmp_path / "audio.m4a")
    assert out.exists() and out.stat().st_size < wav.stat().st_size / 3
    assert not (tmp_path / "audio.part.m4a").exists()
    with av.open(str(out)) as c:
        s = c.streams.audio[0]
        assert s.codec_context.name == "aac" and s.channels == 1
        assert abs(float(c.duration / av.time_base) - secs) < 0.5
