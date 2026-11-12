# End-to-end test (Linux)

Runs the real background service, real audio devices (PulseAudio null sinks:
`micsink` -> `virtmic` as the microphone, `speakers` as the output), a real
"Zoom Meeting" window on Xvfb for meeting detection, and Chromium (Playwright)
for every page of the app, including the phone over HTTPS. Claude is replaced
by `mock_claude.py` (a streaming Messages API stand-in), Whisper by a script
file (`MEETNOTES_E2E_TRANSCRIBER`), because a model download is needed for
real speech recognition.

    apt-get install pulseaudio pulseaudio-utils libportaudio2 libasound2-plugins xdotool xvfb espeak-ng python3-tk
    # PulseAudio + devices: see the commands at the top of e2e.py's docstring in git history, or:
    pulseaudio -D --exit-idle-time=-1
    pactl load-module module-null-sink sink_name=speakers
    pactl load-module module-null-sink sink_name=micsink
    pactl load-module module-remap-source master=micsink.monitor source_name=virtmic
    pactl set-default-sink speakers && pactl set-default-source virtmic
    xvfb-run -a -s "-screen 0 1600x1000x24" python3 tests/e2e/e2e.py /tmp/meetnotes-e2e

It prints PASS/FAIL per check (61 checks) and writes screenshots to <dir>/shots.
