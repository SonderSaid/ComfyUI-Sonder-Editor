"""Report production fidelity acceptance: pytest tests/diagnostics/test_audio_fidelity.py -s.

Synthetic signals only. No assertion requires amplification, clipping or an
unnecessary lossy intermediate. Detailed timing and lifecycle cases live in the
regular audio acceptance suite.
"""
import json
import numpy as np
import pytest
from scipy.io import wavfile
from server import media_helpers as media


@pytest.mark.parametrize('rate', [32000, 44100, 48000, 96000])
def test_repeated_float_hops_preserve_samples(tmp_path, rate):
    original=np.random.default_rng(11).uniform(-1.4,1.4,(2,4096)).astype(np.float32)
    samples=original
    for generation in range(4):
        path=tmp_path/f'hop-{generation}.wav'
        media.write_audio_wav(path,samples,rate)
        samples,actual=media.decode_audio_samples(path,channels=2,mix_to_mono=False)
        assert actual==rate
    error=float(np.max(np.abs(samples-original)))
    print('AUDIO_FIDELITY_FLOAT_HOPS='+json.dumps({'rate':rate,'generations':4,'max_error':error}))
    np.testing.assert_array_equal(samples,original)


@pytest.mark.parametrize('preset',media.SAVE_VIDEO_PRESET_ORDER[:-1])
def test_delivery_sidecar_bypasses_compression(tmp_path,preset):
    rate=48000
    t=np.arange(rate)/rate
    signal=(.6*np.sin(2*np.pi*(200*t+2000*t*t))).astype(np.float32)
    original=np.stack((signal,signal*.31))
    source=tmp_path/'original.wav';media.write_audio_wav(source,original,rate)
    output=tmp_path/('out'+media.output_extension_for_preset(preset))
    sidecar=tmp_path/'sidecar.wav'
    result=media.encode_video(np.zeros((24,32,32,3),np.uint8),preset_id=preset,
                             output_path=str(output),fps=24,audio_path=str(source),audio_sidecar_path=str(sidecar))
    processing=result['audio_processing']
    _,prepared=wavfile.read(sidecar)
    residual=prepared.T-original*processing['gain']
    maximum=float(np.max(np.abs(residual)))
    print('AUDIO_FIDELITY_DELIVERY='+json.dumps({'preset':preset,'sidecar_max_error':maximum,**processing}))
    assert maximum<=1e-7
    assert processing['decoded_sample_peak']<=1
    assert processing['decoded_oversampled_peak']<=1
