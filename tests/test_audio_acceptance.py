"""Production audio acceptance using synthetic media; no personal assets required."""
import copy
import os
import struct
import threading

import numpy as np
import pytest
from scipy.io import wavfile
from scipy.signal import resample_poly

from server import audio_pipeline as audio
from server import media_helpers as media
from server.timeline_state import AudioTrack, LaneConfig, Scene, TimelineProject


def source(tmp_path, name, samples, rate):
    path = tmp_path / name
    media.write_audio_wav(path, samples, rate)
    return name


@pytest.mark.parametrize('rate', [32000, 44100, 48000, 96000])
def test_native_float_roundtrip_without_ffprobe(tmp_path, monkeypatch, rate):
    samples = np.random.default_rng(1).uniform(-1.6, 1.6, (2, 4000)).astype(np.float32)
    path = tmp_path / source(tmp_path, 'native.wav', samples, rate)
    monkeypatch.setattr(media, '_mutagen_audio_metadata', lambda p: (0, 0))
    monkeypatch.setattr(media, '_ffprobe_json', lambda *a, **kw: {})
    decoded, actual = media.decode_audio_samples(path, channels=2, mix_to_mono=False)
    assert actual == rate
    np.testing.assert_array_equal(decoded, samples)


def test_rf64_header_uses_64bit_sizes_without_allocating_large_waveform():
    frames = 2**30
    header = audio.pcm_wav_header(96000, 2, frames)
    assert header[:4] == b'RF64'
    assert header[12:16] == b'ds64'
    assert struct.unpack_from('<Q', header, 28)[0] == frames * 8
    assert struct.unpack_from('<Q', header, 36)[0] == frames


@pytest.mark.parametrize('fps', [24, 25, 30, 24000/1001, 30000/1001])
def test_split_trim_and_nonzero_window_use_one_clock(tmp_path, fps):
    rate = 44100
    samples = np.zeros((2, rate * 3), np.float32)
    samples[:, ::137] = .5
    path = source(tmp_path, 'impulses.wav', samples, rate)
    project = TimelineProject(project_dir=str(tmp_path), fps=24)
    scene = Scene(fps=fps, audio_tracks=[AudioTrack(source_path=path, timeline_start_frame=3,
                          timeline_end_frame=43, source_in_frame=7)])
    whole, actual = audio.scene_audio_samples(project, scene, 0, 48)
    selected, _ = audio.scene_audio_samples(project, scene, 11, 39)
    np.testing.assert_array_equal(selected, whole[:, audio.frame_sample(11,rate,fps):audio.frame_sample(39,rate,fps)])
    original = scene.audio_tracks[0]
    right = copy.deepcopy(original)
    original.timeline_end_frame = 19
    right.timeline_start_frame = 19
    right.source_in_frame += 16
    scene.audio_tracks.append(right)
    split, _ = audio.scene_audio_samples(project, scene, 0, 48)
    np.testing.assert_array_equal(split, whole)
    exported = tmp_path / 'export.wav'
    audio.mix_scene_audio_to_wav(project, scene, 11, 39, exported)
    exported_rate, exported_samples = wavfile.read(exported)
    assert actual == exported_rate == rate
    np.testing.assert_array_equal(exported_samples.T, selected)


def test_scene_rate_includes_audible_sources_outside_selection(tmp_path):
    tracks = []
    for index, rate in enumerate([32000,44100,48000,96000]):
        path = source(tmp_path, f'{rate}.wav', np.full((2,rate),.1,np.float32), rate)
        tracks.append(AudioTrack(source_path=path, timeline_start_frame=index*24, timeline_end_frame=(index+1)*24))
    project = TimelineProject(project_dir=str(tmp_path),fps=24)
    scene = Scene(audio_tracks=tracks)
    mixed, rate = audio.scene_audio_samples(project, scene, 0, 12)
    assert rate == 96000 and mixed.shape == (2, 48000)
    assert np.mean(mixed[:, 256:-256]) == pytest.approx(.1, abs=1e-6)
    tracks[-1].volume = 0
    assert audio.scene_audio_spec(project,scene,0,12)[0] == 48000
    tracks[-2].muted = True
    assert audio.scene_audio_spec(project,scene,0,12)[0] == 44100


def test_effective_volumes_mute_hidden_and_overrange(tmp_path):
    path = source(tmp_path, 'source.wav', np.ones((2,48000),np.float32),48000)
    tracks = [AudioTrack(source_path=path,timeline_end_frame=24,volume=value,lane_index=i)
              for i,value in enumerate([.75, 4, 0, -2, 1, 1])]
    tracks[4].muted = True
    scene = Scene(audio_tracks=tracks,audio_lane_configs=[LaneConfig(hidden=i==5) for i in range(6)])
    project = TimelineProject(project_dir=str(tmp_path),fps=24)
    mixed, _ = audio.scene_audio_samples(project,scene,0,24)
    np.testing.assert_array_equal(mixed, np.full((2,48000),1.75,np.float32))
    assert [t.volume for t in tracks] == [.75,4,0,-2,1,1]
    from server.timeline_export import _audio_sources
    assert [s['volume'] for s in _audio_sources(project,scene,0,24)] == [.75,1,0,0]


def test_peak_overlap_agrees_with_whole_signal(tmp_path):
    samples = np.random.default_rng(9).normal(0,.2,(14000,2)).astype(np.float32)
    samples[4095] = (1.1,-.4)
    path=tmp_path/'peak.wav';media.write_audio_wav(path,samples.T,48000)
    measured=audio.measure_peaks(path,block_size=4096)
    whole=resample_poly(samples.astype(np.float64),4,1,axis=0,window=('kaiser',10))
    assert measured['oversampled_peak'] == pytest.approx(np.max(np.abs(whole)),abs=1e-12)
    assert measured['sample_peak'] == np.max(np.abs(samples))


@pytest.mark.parametrize('amplitude', [0, .2, 1.4])
def test_linked_gain_and_sidecar_are_faithful(tmp_path, amplitude):
    t=np.arange(16000)/32000
    samples=np.stack((amplitude*np.sin(2*np.pi*500*t),amplitude*.23*np.sin(2*np.pi*1100*t))).astype(np.float32)
    original=samples.copy()
    path=tmp_path/source(tmp_path,'source.wav',samples,32000)
    sidecar=tmp_path/'sidecar.wav'
    frames=np.zeros((12,32,32,3),np.uint8)
    result=media.encode_video(frames,preset_id='Editing Master MP4',output_path=str(tmp_path/'out.mp4'),
                              fps=24,audio_path=str(path),audio_sidecar_path=str(sidecar))['audio_processing']
    rate,prepared=wavfile.read(sidecar)
    assert rate==32000
    np.testing.assert_allclose(prepared.T,original*result['gain'],atol=1e-7)
    np.testing.assert_array_equal(samples,original)
    np.testing.assert_array_equal(wavfile.read(path)[1].T,original)
    assert result['gain']==(1 if amplitude<=.2 else pytest.approx(audio.HEADROOM/result['measured_peak']))
    assert result['decoded_oversampled_peak']<=1


@pytest.mark.parametrize('preset,codec,bits', [
    ('Compatible MP4','aac',None),('High Quality MP4','aac',None),
    ('Editing Master MP4','flac',24),('ProRes 422 HQ','pcm_s24le',24),('Lossless FFV1 (RGB)','flac',24)])
def test_presets_actual_codec_precision_and_full_video_length(tmp_path,preset,codec,bits):
    # Supplied audio is shorter than video, and the waveform has non-16-bit detail.
    samples=np.random.default_rng(8).normal(0,.03,(2,7000)).astype(np.float32)
    path=tmp_path/source(tmp_path,'source.wav',samples,48000)
    output=tmp_path/('out'+media.output_extension_for_preset(preset));sidecar=tmp_path/'sidecar.wav'
    result=media.encode_video(np.zeros((12,32,32,3),np.uint8),preset_id=preset,
                             output_path=str(output),fps=24,audio_path=str(path),audio_sidecar_path=str(sidecar))
    text=media._ffmpeg_input_text(output)
    assert f'Audio: {codec}' in text
    if bits: assert '(24 bit)' in text
    assert result['audio_processing']['delivery_bits']==bits
    rate,prepared=wavfile.read(sidecar)
    assert prepared.shape==(24000,2)
    np.testing.assert_array_equal(prepared[:7000].T,samples)
    assert not np.any(prepared[7000:])
    import cv2
    cap=cv2.VideoCapture(str(output))
    try: assert cap.get(cv2.CAP_PROP_FRAME_COUNT)==12
    finally: cap.release()
    if bits:
        decoded,_=media.decode_audio_samples(output,channels=2,mix_to_mono=False)
        assert np.max(np.abs(decoded[:,:7000]-samples)) <= 2/(2**23)


def test_cancelled_delivery_never_encodes_video_or_leaves_output(tmp_path,monkeypatch):
    path=tmp_path/source(tmp_path,'source.wav',np.zeros((2,4800),np.float32),48000)
    event=threading.Event();event.set()
    monkeypatch.setattr(media,'_run_ffmpeg_streaming_frames',lambda *a,**k:pytest.fail('video ran before verification'))
    output=tmp_path/'out.mp4'
    with pytest.raises(media.MediaOperationCancelled):
        media.encode_video(np.zeros((12,32,32,3),np.uint8),preset_id='Compatible MP4',output_path=str(output),
                           fps=24,audio_path=str(path),cancel_event=event)
    assert not output.exists()


def test_verification_retries_from_original_and_fails_before_video(tmp_path,monkeypatch):
    path=tmp_path/source(tmp_path,'source.wav',np.full((2,4800),.1,np.float32),48000)
    real=audio.measure_peaks;calls=[]
    def force_overload(path,**kwargs):
        result=real(path,**kwargs)
        if os.path.basename(path)=='decoded.wav':
            calls.append(result)
            result['peak']=1.2
        return result
    monkeypatch.setattr(audio,'measure_peaks',force_overload)
    monkeypatch.setattr(media,'_run_ffmpeg_streaming_frames',lambda *a,**k:pytest.fail('unverified video'))
    with pytest.raises(RuntimeError,match='two corrective attempts'):
        media.encode_video(np.zeros((12,32,32,3),np.uint8),preset_id='Compatible MP4',
                           output_path=str(tmp_path/'out.mp4'),fps=24,audio_path=str(path))
    assert len(calls)==3
    assert calls[0]['sample_peak']>calls[1]['sample_peak']>calls[2]['sample_peak']
    assert not (tmp_path/'out.mp4').exists()

@pytest.mark.parametrize('codec,bits', [('pcm_s16le',16), ('pcm_s24le',24), ('alac',24), ('flac',16)])
def test_final_integer_rounding_dither_and_custom_precision(tmp_path, codec, bits):
    samples=np.random.default_rng(91).uniform(-.3,.3,(2,10000)).astype(np.float32)
    path=tmp_path/source(tmp_path,'source.wav',samples,96000)
    container={'pcm_s16le':'wav','pcm_s24le':'wav','alac':'m4a','flac':'flac'}[codec]
    output=tmp_path/f'out.{container}'
    result=media.encode_audio(str(path),str(output),codec=codec,container=container)
    decoded,rate=media.decode_audio_samples(output,channels=2,mix_to_mono=False)
    assert rate==96000 and result['audio_processing']['delivery_bits']==bits
    error=decoded-samples
    quantum=2**(-(bits-1))
    assert np.max(np.abs(error))<=2*quantum
    assert abs(np.mean(error))<.05*quantum
    # Final integer samples lie on the intended PCM grid.
    np.testing.assert_array_equal(decoded/quantum,np.rint(decoded/quantum))


def test_imported_video_rate_and_float_extraction_without_probe(tmp_path,monkeypatch):
    samples=np.full((2,3200),.1234567,np.float32)
    path=tmp_path/source(tmp_path,'source.wav',samples,32000)
    video=tmp_path/'input.mov'
    media.run_ffmpeg_command([media.get_ffmpeg_path(),'-y','-f','lavfi','-i','color=s=32x32:r=24:d=0.1',
                              '-i',str(path),'-c:v','libx264','-c:a','pcm_f32le',str(video)],timeout=30)
    monkeypatch.setattr(media,'_ffprobe_json',lambda *a,**k:{})
    monkeypatch.setattr(media,'_mutagen_audio_metadata',lambda *a:(0,0))
    metadata=media.probe_media_metadata(video,'video',strict=True)
    assert metadata['has_audio'] and metadata['sample_rate']==32000
    decoded,rate=media.decode_audio_samples(video,channels=2,mix_to_mono=False)
    assert rate==32000
    np.testing.assert_array_equal(decoded,samples)


def test_unknown_rate_render_does_not_backfill_project(tmp_path):
    from server.timeline_state import Asset
    path=source(tmp_path,'unknown.wav',np.zeros((2,3200),np.float32),32000)
    asset=Asset(path=path,asset_type='audio',sample_rate=0)
    project=TimelineProject(project_dir=str(tmp_path),assets=[asset])
    scene=Scene(audio_tracks=[AudioTrack(source_path=path,timeline_end_frame=2)])
    project.scenes=[scene]
    before=copy.deepcopy(project.to_dict())
    audio.scene_audio_samples(project,scene,0,2)
    assert project.to_dict()==before and asset.sample_rate==0


def test_unprobeable_source_only_blocks_a_window_using_it(tmp_path, caplog, monkeypatch):
    good = source(tmp_path, 'good.wav', np.full((2, 48000), .125, np.float32), 48000)
    (tmp_path / 'broken.wav').write_bytes(b'not audio')
    project = TimelineProject(project_dir=str(tmp_path), fps=24)
    scene = Scene(audio_tracks=[
        AudioTrack(source_path=good, timeline_end_frame=24),
        AudioTrack(source_path='broken.wav', timeline_start_frame=72000, timeline_end_frame=72024),
        AudioTrack(source_path='broken.wav', timeline_start_frame=72024, timeline_end_frame=72048),
    ])
    probe = media.probe_audio_metadata
    calls = []
    def counted(path):
        calls.append(os.path.basename(path))
        return probe(path)
    monkeypatch.setattr(media, 'probe_audio_metadata', counted)
    samples, rate = audio.scene_audio_samples(project, scene, 0, 24)
    np.testing.assert_array_equal(samples, np.full((2, 48000), .125, np.float32))
    assert rate == 48000
    assert calls.count('broken.wav') == 1
    assert caplog.text.count('Skipping unprobeable audio source outside output window:') == 1
    with pytest.raises(media.MediaProbeError, match='broken.wav'):
        audio.scene_audio_samples(project, scene, 72000, 72024)
    # A duplicate path first encountered outside the window must still fail inside it.
    with pytest.raises(media.MediaProbeError, match='broken.wav'):
        audio.scene_audio_samples(project, scene, 72024, 72048)


@pytest.mark.parametrize('rf64', ['auto', 'always'])
def test_ffmpeg_float_wav_chunk_reader_roundtrip(tmp_path, monkeypatch, rf64):
    samples = np.random.default_rng(16).uniform(-1.4, 1.4, (2, 24000)).astype(np.float32)
    native = tmp_path / source(tmp_path, 'native.wav', samples, 48000)
    path = tmp_path / 'ffmpeg.wav'
    media.run_ffmpeg_command([media.get_ffmpeg_path(), '-y', '-i', str(native),
                              '-c:a', 'pcm_f32le', '-rf64', rf64, str(path)])
    assert path.read_bytes()[:4] == (b'RF64' if rf64 == 'always' else b'RIFF')
    with audio.mapped_float_wav(path) as (rate, mapped):
        assert rate == 48000 and isinstance(mapped, np.memmap)
        np.testing.assert_array_equal(mapped.T, samples)
    assert mapped._mmap.closed
    assert audio.measure_peaks(path)['sample_peak'] == np.max(np.abs(samples))
    # Exercise the materializing Editor seam with the same FFmpeg RF64 dialect.
    import shutil
    monkeypatch.setattr(audio, 'mix_scene_audio_to_wav', lambda *args, **kwargs: shutil.copyfile(path, args[4]))
    output, rate = audio.scene_audio_samples(None, None, 0, 12)
    np.testing.assert_array_equal(output, samples)
    path.unlink()  # The context seam must close the map and own a detached tensor copy.
    np.testing.assert_array_equal(output, samples)


def test_float_chunk_reader_odd_padding_empty_and_truncated_data(tmp_path):
    native = tmp_path / source(tmp_path, 'native.wav', np.array([[.125, 1.5]], np.float32), 32000)
    content = native.read_bytes()
    junk = b'JUNK' + struct.pack('<I', 3) + b'abc' + b'\0'
    content = content[:4] + struct.pack('<I', len(content) - 8 + len(junk)) + content[8:12] + junk + content[12:]
    native.write_bytes(content)
    with audio.mapped_float_wav(native) as (rate, mapped):
        assert rate == 32000
        np.testing.assert_array_equal(mapped[:, 0], [.125, 1.5])
    native.write_bytes(content[:-1])
    with pytest.raises(ValueError, match='Truncated'):
        with audio.mapped_float_wav(native):
            pass
    media.write_audio_wav(native, np.empty((2, 0), np.float32), 96000)
    with audio.mapped_float_wav(native) as (rate, mapped):
        assert rate == 96000 and mapped.shape == (0, 2)


def test_silent_integer_delivery_stays_digital_silence(tmp_path):
    path = tmp_path / source(tmp_path, 'silence.wav', np.zeros((2, 480000), np.float32), 48000)
    output = tmp_path / 'silence.flac'
    result = media.encode_audio(str(path), str(output), codec='flac', container='flac', bits=24)
    samples, rate = media.decode_audio_samples(output, channels=2, mix_to_mono=False)
    assert rate == 48000 and not np.any(samples)
    assert output.stat().st_size < 15000
    assert result['audio_processing']['gain'] == 1
    assert result['audio_processing']['dither'] is None


def test_preset_audio_descriptions_and_documentation_match_delivery():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    js = (root / 'web/js/editor_settings.js').read_text(encoding='utf-8')
    js = js.split('export const SAVE_PRESET_OPTIONS = [', 1)[1].split('];', 1)[0]
    descriptions = dict(re.findall(r'value: "([^"]+)"[^\n]*description: "([^"]+)"', js))
    # The audio-delivery table lives with the Save Video / Export preset docs, not
    # in the README: the README copy was redundant and said nothing the reader
    # could act on. Keep this pointed at the page that explains the presets.
    doc_path = root / 'docs/assets-and-gallery.md'
    rows = dict(re.findall(r'^\| ([^|]+?) \| ([^|]+?) \|$', doc_path.read_text(encoding='utf-8'), re.M))
    expected = [n for n in media.SAVE_VIDEO_PRESETS if n != media.CUSTOM_SAVE_VIDEO_PRESET]
    missing = [n for n in expected if n not in rows]
    assert not missing, f'{doc_path} is missing audio-delivery rows for {missing}'
    for name, preset in media.SAVE_VIDEO_PRESETS.items():
        if name == media.CUSTOM_SAVE_VIDEO_PRESET:
            continue
        spec = media.audio_only_export_spec(name)
        codec = 'PCM' if spec['codec'].startswith('pcm_') else spec['codec'].upper()
        precision = str(spec['bits'] or spec['bitrate_kbps'])
        for text in (descriptions[name], preset['description'], rows[name]):
            assert codec in text and precision in text, (name, text, spec)
    assert media.audio_only_export_spec('Editing Master MP4')['extension'] == '.flac'


@pytest.mark.parametrize('codec,bits', [('aac', None), ('pcm_s16le', 16), ('flac', 16)])
def test_audio_only_custom_precision_keeps_existing_meaning(codec, bits):
    assert media.audio_only_export_spec('Custom', {'custom_audio_codec': codec})['bits'] == bits


def test_audio_only_precision_follows_preset_args_not_name(monkeypatch):
    preset = dict(media.SAVE_VIDEO_PRESETS['Compatible MP4'], audio_args=['-c:a', 'flac', '-bits_per_raw_sample', '24'])
    monkeypatch.setitem(media.SAVE_VIDEO_PRESETS, 'Compatible MP4', preset)
    assert media.audio_only_export_spec('Compatible MP4')['bits'] == 24


@pytest.mark.parametrize('major,accepted', [(60, False), (61, True), (62, True)])
def test_audio_ffmpeg_floor_names_the_selected_binary(monkeypatch, major, accepted):
    from types import SimpleNamespace
    path = f'ffmpeg-test-library-{major}'
    monkeypatch.setattr(media.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0,
        stdout=f'ffmpeg version development\nlibavformat    {major}.  1.100 / {major}.  1.100'))
    media.require_audio_ffmpeg.cache_clear()
    try:
        if accepted:
            assert media.require_audio_ffmpeg(path) == path
        else:
            with pytest.raises(RuntimeError, match=f'FFmpeg 7.0.*{path}'):
                media.require_audio_ffmpeg(path)
    finally:
        media.require_audio_ffmpeg.cache_clear()


def test_unity_delivery_avoids_a_copy_and_retry_keeps_original(tmp_path, monkeypatch):
    import hashlib
    source_path = tmp_path / source(tmp_path, 'source.wav', np.full((2, 4800), .1, np.float32), 48000)
    work = tmp_path / 'work'
    work.mkdir()
    original_hashes, renders = [], []
    real_render, real_measure = audio.render_float_file, audio.measure_peaks
    def render(src, target, **kwargs):
        renders.append((os.path.basename(src), os.path.basename(target)))
        if os.path.basename(src) == 'original.wav':
            original_hashes.append(hashlib.sha256(open(src, 'rb').read()).hexdigest())
        return real_render(src, target, **kwargs)
    monkeypatch.setattr(audio, 'render_float_file', render)
    with audio.prepared_audio(source_path, ['-c:a', 'aac'], work_dir=str(work)) as (encoded, prepared, result):
        assert os.path.basename(prepared) == 'original.wav'
        assert result['gain'] == 1 and result['corrective_attempts'] == 0
        assert not any(target == 'prepared.wav' for _, target in renders)
        assert os.path.commonpath([str(work), prepared]) == str(work)
    assert list(work.iterdir()) == []
    calls = 0
    def measure(path, **kwargs):
        nonlocal calls
        measured = real_measure(path, **kwargs)
        if os.path.basename(path) == 'decoded.wav':
            calls += 1
            if calls <= 2:
                measured['peak'] = 1.2
        return measured
    monkeypatch.setattr(audio, 'measure_peaks', measure)
    with audio.prepared_audio(source_path, ['-c:a', 'aac'], work_dir=str(work)) as (_, prepared, result):
        assert result['corrective_attempts'] == 2 and result['gain'] < 1
        assert os.path.basename(prepared) == 'prepared.wav'
    assert len(original_hashes) == 2 and len(set(original_hashes)) == 1
    assert list(work.iterdir()) == []


def test_editor_audio_uses_configured_temp_volume(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import sys
    work = tmp_path / 'configured'
    monkeypatch.setitem(sys.modules, 'folder_paths', SimpleNamespace(get_temp_directory=lambda: str(work)))
    observed = []
    real = audio.mix_scene_audio_to_wav
    def capture(*args, **kwargs):
        observed.append(args[4])
        return real(*args, **kwargs)
    monkeypatch.setattr(audio, 'mix_scene_audio_to_wav', capture)
    audio.scene_audio_samples(TimelineProject(project_dir=str(tmp_path)), Scene(), 0, 1)
    assert os.path.commonpath([str(work), observed[0]]) == str(work)
    assert list(work.iterdir()) == []


def test_video_encoder_failure_does_not_trigger_audio_fallback(tmp_path, monkeypatch):
    path = tmp_path / source(tmp_path, 'source.wav', np.full((2, 4800), .1, np.float32), 48000)
    calls = []
    def failed_video(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('video encoder failed')
    monkeypatch.setattr(media, '_run_ffmpeg_streaming_frames', failed_video)
    with pytest.raises(RuntimeError, match='video encoder failed'):
        media.encode_video(np.zeros((4, 32, 32, 3), np.uint8), preset_id='Compatible MP4',
            output_path=str(tmp_path / 'out.mp4'), fps=24, audio_path=str(path), allow_audio_fallback=True)
    assert len(calls) == 1 and not (tmp_path / 'out.mp4').exists()


def test_custom_none_ignores_supplied_audio_without_preparing_it(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, 'prepared_audio', lambda *a, **k: pytest.fail('explicit none must not prepare audio'))
    output = tmp_path / 'silent.mp4'
    result = media.encode_video(np.zeros((4, 32, 32, 3), np.uint8), preset_id='Custom',
        custom_options={'custom_audio_codec': 'none'}, output_path=str(output), fps=24,
        audio_path=str(tmp_path / 'missing.wav'), allow_audio_fallback=True)
    assert not result['has_audio'] and not result.get('alerts')
    assert 'Audio:' not in media._ffmpeg_input_text(output)


def test_locked_partial_sidecar_does_not_discard_valid_video(tmp_path, monkeypatch):
    path = tmp_path / source(tmp_path, 'source.wav', np.full((2, 4800), .1, np.float32), 48000)
    output, sidecar = tmp_path / 'out.mp4', tmp_path / 'sidecar.wav'
    original_remove = os.remove
    def locked_remove(path):
        if str(path) == str(sidecar):
            raise PermissionError('test locked sidecar')
        return original_remove(path)
    def failed_copy(src, target, **kwargs):
        sidecar.write_bytes(b'partial')
        raise OSError('test copy failure')
    monkeypatch.setattr(audio, 'copy_audio_file', failed_copy)
    monkeypatch.setattr(os, 'remove', locked_remove)
    result = media.encode_video(np.zeros((4, 32, 32, 3), np.uint8), preset_id='Compatible MP4',
        output_path=str(output), fps=24, audio_path=str(path), audio_sidecar_path=str(sidecar))
    assert output.exists() and result['has_audio'] and result['alerts']
    assert result['audio_sidecar_ready'] is False


@pytest.mark.parametrize('container', ['wav', 'mov'])
def test_mono_stereo_decode_preserves_native_level_without_ffprobe(tmp_path, monkeypatch, container):
    samples = np.array([[.25, -.125, 1.25, -1.375] * 1000], np.float32)
    path = tmp_path / source(tmp_path, 'mono.wav', samples, 32000)
    if container == 'mov':
        video = tmp_path / 'mono.mov'
        media.run_ffmpeg_command([media.get_ffmpeg_path(), '-y', '-f', 'lavfi', '-i', 'color=s=32x32:r=24:d=0.125',
            '-i', str(path), '-c:v', 'libx264', '-c:a', 'pcm_f32le', str(video)], timeout=30)
        path = video
    monkeypatch.setattr(media, '_ffprobe_json', lambda *a, **k: {})
    monkeypatch.setattr(media, '_mutagen_audio_metadata', lambda *a: (0, 0))
    decoded, rate = media.decode_audio_samples(path, channels=2, mix_to_mono=False)
    assert rate == 32000
    np.testing.assert_array_equal(decoded, np.repeat(samples, 2, axis=0))


def test_mono_dialogue_and_stereo_music_keep_authored_balance(tmp_path):
    mono = source(tmp_path, 'dialogue.wav', np.full((1, 48000), .25, np.float32), 48000)
    stereo = source(tmp_path, 'music.wav', np.tile([[.0625], [-.125]], (1, 48000)).astype(np.float32), 48000)
    scene = Scene(audio_tracks=[
        AudioTrack(source_path=mono, timeline_end_frame=12, volume=.5),
        AudioTrack(source_path=stereo, timeline_end_frame=24, volume=.25, lane_index=1),
    ])
    project = TimelineProject(project_dir=str(tmp_path), fps=24)
    selected, rate = audio.scene_audio_samples(project, scene, 6, 18)
    expected = np.empty((2, 24000), np.float32)
    expected[:, :12000] = np.array([[.140625], [.09375]], np.float32)
    expected[:, 12000:] = np.array([[.015625], [-.03125]], np.float32)
    assert rate == 48000
    np.testing.assert_array_equal(selected, expected)
    path = tmp_path / 'export.wav'
    audio.mix_scene_audio_to_wav(project, scene, 6, 18, path)
    with audio.mapped_float_wav(path) as (export_rate, exported):
        assert export_rate == rate
        np.testing.assert_array_equal(np.array(exported.T, copy=True), expected)
