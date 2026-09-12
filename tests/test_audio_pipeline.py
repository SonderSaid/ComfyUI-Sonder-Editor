"""Audible-path regressions using real FFmpeg and disposable media."""
import numpy as np
from scipy.io import wavfile
from server.timeline_renderer import mix_scene_audio_to_wav
from server.timeline_state import AudioTrack, Scene, TimelineProject


def test_ending_clips_do_not_raise_remaining_audio(tmp_path):
    rate = 48000
    wavfile.write(tmp_path / "long.wav", rate, np.full((rate * 5, 2), .1, np.float32))
    wavfile.write(tmp_path / "short.wav", rate, np.full((rate, 2), .05, np.float32))
    project = TimelineProject(project_dir=str(tmp_path), fps=24)
    scene = Scene(audio_tracks=[
        AudioTrack(source_path="long.wav", timeline_start_frame=0, timeline_end_frame=120),
        AudioTrack(source_path="short.wav", timeline_start_frame=0, timeline_end_frame=24),
    ])
    output = tmp_path / "mix.wav"
    mix_scene_audio_to_wav(project, scene, 0, 120, str(output))
    actual_rate, audio = wavfile.read(output)
    if audio.dtype.kind == "i":
        audio = audio.astype(np.float32) / 32768
    np.testing.assert_allclose(audio[actual_rate // 2], .15, atol=4e-5)
    np.testing.assert_allclose(audio[actual_rate * 4], .1, atol=4e-5)
