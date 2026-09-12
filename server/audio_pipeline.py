"""Audio computation only; project mutation and generated-asset lifecycle stay caller-owned."""
from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
import logging
import math
import os
import struct
import tempfile

import numpy as np
from scipy.signal import resample_poly

from . import media_helpers as media
from .lane_registry import hidden_lane_indexes
from .path_security import resolve_existing_project_path
from .timeline_state import effective_scene_fps

logger = logging.getLogger("sonder_editor")

BLOCK_SAMPLES = 65536
PEAK_OVERLAP = 64  # More than the 10 input samples of support in resample_poly's FIR.
HEADROOM = 10 ** (-1 / 20)


def check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise media.MediaOperationCancelled("Audio processing cancelled")


def copy_audio_file(source, target, *, cancel_event=None):
    """Copy a prepared sidecar or verified stream with bounded cancellation latency."""
    check_cancel(cancel_event)
    with open(source, "rb") as reader, open(target, "wb") as writer:
        while True:
            check_cancel(cancel_event)
            block = reader.read(1024 * 1024)
            if not block:
                break
            writer.write(block)
    check_cancel(cancel_event)


def frame_sample(frame, rate, fps):
    """Half-up rounding on the clock whose origin is scene frame zero."""
    value = Fraction(int(frame) * int(rate), 1) / Fraction(str(fps))
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def effective_volume(track):
    value = getattr(track, "volume", 1.0)
    value = 1.0 if value is None else float(value)
    return min(1.0, max(0.0, value)) if math.isfinite(value) else 1.0


def resample_filter(rate):
    # Built-in swresample, available without an optional libsoxr build.
    return f"aformat=sample_fmts=flt,aresample={int(rate)}:osf=flt:tsf=fltp:filter_size=128:cutoff=0.95"


def stereo_filter():
    """Retain mono amplitude when adapting to the browser's stereo mix.

    Negotiate native mono/stereo first, then duplicate center at unity. A direct
    stereo format request instead applies FFmpeg's -3 dB mono coefficient.
    Other layouts keep the existing FFmpeg downmix before this channel mapping.
    """
    return "aformat=channel_layouts=stereo|mono,pan=stereo|FL=FL+FC|FR=FR+FC"


def audible_sources(project, scene):
    hidden = hidden_lane_indexes(scene, "audio")
    for track in getattr(scene, "audio_tracks", []) or []:
        if track.muted or track.lane_index in hidden:
            continue
        path = resolve_existing_project_path(project, track.source_path, purpose="scene audio source")
        if os.path.isfile(path):
            yield track, path
        else:
            logger.warning("Skipping missing audio source: %s", track.source_path)


def scene_audio_spec(project, scene, start, end):
    sources = list(audible_sources(project, scene))
    # Classify overlap before probing: one path can have several placements.
    # Read actual media, never repair persisted metadata as a rendering side effect.
    window_paths, vote_paths = set(), dict.fromkeys(
        path for track, path in sources
        if effective_volume(track) > 0 and track.timeline_end_frame > track.timeline_start_frame
    )
    for track, path in sources:
        if effective_volume(track) > 0 and max(start, track.timeline_start_frame) < min(end, track.timeline_end_frame):
            window_paths.add(path)
    rates = {}
    for path in vote_paths:
        try:
            rates[path] = int(media.probe_audio_metadata(path)["sample_rate"])
        except media.MediaProbeError as exc:
            if path in window_paths:
                raise media.MediaProbeError(f"Could not read audio source '{os.path.basename(path)}': {exc}") from exc
            logger.warning("Skipping unprobeable audio source outside output window: %s (%s)", path, exc)
    rate = max(rates.values(), default=48000)
    fps = effective_scene_fps(project, scene)
    origin = frame_sample(start, rate, fps)
    length = max(0, frame_sample(end, rate, fps) - origin)
    contributors = []
    for track, path in sources:
        left = max(start, int(track.timeline_start_frame))
        right = min(end, int(track.timeline_end_frame))
        if right <= left:
            continue
        # The source origin is invariant when a track is split. Subtract rounded
        # endpoints, not independently rounded durations, at both boundaries.
        source_origin = frame_sample(int(track.timeline_start_frame) - int(track.source_in_frame or 0), rate, fps)
        source_start = frame_sample(left, rate, fps) - source_origin
        count = frame_sample(right, rate, fps) - frame_sample(left, rate, fps)
        contributors.append({
            "track": track, "path": path, "volume": effective_volume(track),
            "source_start_sample": max(0, source_start),
            "sample_count": max(0, count + min(0, source_start)),
            "delay_samples": frame_sample(left, rate, fps) - origin - min(0, source_start),
        })
    return rate, length, contributors


def active_contributors(contributors):
    """The shared audibility predicate; zero-volume provenance remains preserved."""
    return [entry for entry in contributors if entry["volume"] > 0 and entry["sample_count"] > 0]


def mix_scene_audio_to_wav(project, scene, start_frame, end_frame, output_wav, *, cancel_event=None):
    """Stream the unbounded stereo float mix for [start_frame, end_frame)."""
    check_cancel(cancel_event)
    rate, length, contributors = scene_audio_spec(project, scene, start_frame, end_frame)
    os.makedirs(os.path.dirname(os.path.abspath(output_wav)), exist_ok=True)
    if length == 0:
        write_float_wav(output_wav, np.empty((2, 0), np.float32), rate)
        return contributors
    media.require_audio_ffmpeg(media.get_ffmpeg_path())
    active = active_contributors(contributors)
    cmd = [media.get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y"]
    filters, labels = [], []
    for index, entry in enumerate(active):
        cmd += ["-i", entry["path"]]
        first = entry["source_start_sample"]
        count = entry["sample_count"]
        # Equal delay on every channel uses FFmpeg's chunked leading silence,
        # avoiding a per-channel ring buffer proportional to timeline position.
        filters.append(
            f"[{index}:a:0]{resample_filter(rate)},{stereo_filter()},"
            f"atrim=start_sample={first}:end_sample={first + count},asetpts=PTS-STARTPTS,"
            f"volume={entry['volume']:.17g}:precision=float,adelay={entry['delay_samples']}S:all=1[a{index}]"
        )
        labels.append(f"[a{index}]")
    silence_index = len(active)
    cmd += ["-f", "lavfi", "-i", f"anullsrc=r={rate}:cl=stereo"]
    filters.append(f"[{silence_index}:a]atrim=end_sample={length},aformat=sample_fmts=flt[silence]")
    labels.append("[silence]")
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
                   f"apad=whole_len={length},atrim=end_sample={length},aformat=sample_fmts=flt[mix]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[mix]", "-c:a", "pcm_f32le", "-rf64", "auto", str(output_wav)]
    try:
        media.run_ffmpeg_command(cmd, timeout=max(60, length / rate + 60), cancel_event=cancel_event)
        check_cancel(cancel_event)
    except BaseException:
        if os.path.isfile(output_wav):
            os.remove(output_wav)
        raise
    return contributors


def scene_audio_samples(project, scene, start, end, *, work_dir=None):
    with tempfile.TemporaryDirectory(prefix="sonder_audio_context_", dir=work_dir or media.transient_temp_dir()) as directory:
        path = os.path.join(directory, "mix.wav")
        mix_scene_audio_to_wav(project, scene, start, end, path)
        with mapped_float_wav(path) as (rate, samples):
            # Copy before closing the Windows mapping, including mono/empty inputs.
            return np.array(samples.T, dtype=np.float32, order="C", copy=True), rate





def pcm_wav_header(sample_rate, channels, frames, *, bits=32, floating=True):
    """RIFF/RF64 header for prepared float or final integer PCM."""
    block_align = int(channels) * (bits // 8)
    size = int(frames) * block_align
    fmt = struct.pack("<HHIIHH", 3 if floating else 1, channels, sample_rate,
                      sample_rate * block_align, block_align, bits)
    if floating:
        fmt += struct.pack("<H", 0)
    fact = b"fact" + struct.pack("<II", 4, min(frames, 0xFFFFFFFF)) if floating else b""
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt + fact
    riff_size = 4 + len(chunks) + 8 + size + size % 2
    data_size = min(size, 0xFFFFFFFF)
    if riff_size <= 0xFFFFFFFF:
        return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + chunks + b"data" + struct.pack("<I", data_size)
    ds64 = b"ds64" + struct.pack("<IQQQI", 28, riff_size + 36, size, frames, 0)
    return b"RF64\xff\xff\xff\xffWAVE" + ds64 + chunks + b"data\xff\xff\xff\xff"


def write_integer_delivery(source, target, bits, *, cancel_event=None, dither=True):
    """Round with TPDF once, before any lossless integer codec sees the samples.

    A 24-bit encoder drops the bottom eight bits of an s32 input. Asking FFmpeg
    to dither to 24 bits in that carrier still leaves a half-LSB truncation bias.
    Quantize explicitly to the final grid, then feed exact integer PCM instead.
    """
    if bits not in (16, 24):
        raise ValueError("Integer audio delivery supports 16 or 24 bits")
    rng = np.random.default_rng(0)
    scale = 2 ** (bits - 1)
    with mapped_float_wav(source) as (rate, samples), open(target, "wb") as writer:
        writer.write(pcm_wav_header(rate, samples.shape[1], len(samples), bits=bits, floating=False))
        for start in range(0, len(samples), BLOCK_SAMPLES):
            check_cancel(cancel_event)
            block = np.asarray(samples[start:start + BLOCK_SAMPLES], dtype=np.float64)
            noise = rng.random(block.shape) - rng.random(block.shape) if dither else 0.0
            rounded = np.rint(block * scale + noise)
            if not np.isfinite(rounded).all() or np.any(rounded < -scale) or np.any(rounded > scale - 1):
                raise ValueError("Prepared audio exceeds the final integer PCM range")
            if bits == 16:
                writer.write(rounded.astype("<i2").tobytes())
            else:
                carrier = rounded.astype("<i4").view(np.uint8).reshape(-1, 4)
                writer.write(carrier[:, :3].tobytes())
        if (len(samples) * samples.shape[1] * (bits // 8)) % 2:
            writer.write(b"\x00")



def write_float_wav(path, samples, sample_rate):
    arr = np.asarray(samples)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.ndim != 2 or arr.shape[0] < 1 or int(sample_rate) <= 0:
        raise ValueError("Audio requires channel-first samples and a positive native rate")
    with open(path, "wb") as output:
        output.write(pcm_wav_header(int(sample_rate), arr.shape[0], arr.shape[1]))
        for start in range(0, arr.shape[1], BLOCK_SAMPLES):
            block = np.asarray(arr[:, start:start + BLOCK_SAMPLES].T, dtype="<f4")
            if not np.isfinite(block).all():
                raise ValueError("Audio contains non-finite samples")
            output.write(block.tobytes())


@contextmanager
def mapped_float_wav(path):
    """Map float32 RIFF/RF64 without loading the waveform or depending on SciPy's version."""
    with open(path, "rb") as reader:
        file_size = os.fstat(reader.fileno()).st_size
        header = reader.read(12)
        if len(header) != 12 or header[:4] not in (b"RIFF", b"RF64") or header[8:] != b"WAVE":
            raise ValueError("Expected a RIFF/RF64 float32 WAV")
        rf64 = header[:4] == b"RF64"
        limit = file_size if rf64 else min(file_size, struct.unpack_from("<I", header, 4)[0] + 8)
        data_size_64, chunk_sizes, fmt, data = None, {}, None, None
        while reader.tell() + 8 <= limit:
            chunk_id, size = struct.unpack("<4sI", reader.read(8))
            offset = reader.tell()
            if size == 0xFFFFFFFF:
                sizes = chunk_sizes.get(chunk_id, [])
                size = data_size_64 if chunk_id == b"data" else (sizes.pop(0) if sizes else None)
                if size is None:
                    raise ValueError("RF64 chunk has no ds64 size")
            if offset + size > limit:
                raise ValueError("Truncated WAV chunk")
            if chunk_id == b"ds64":
                if not rf64 or size < 28:
                    raise ValueError("Invalid RF64 ds64 chunk")
                riff_size, data_size_64, _, table_count = struct.unpack("<QQQI", reader.read(28))
                if riff_size + 8 > file_size or riff_size + 8 < offset + size or 28 + table_count * 12 > size:
                    raise ValueError("Invalid RF64 size table")
                limit = riff_size + 8
                for _ in range(table_count):
                    key, value = struct.unpack("<4sQ", reader.read(12))
                    chunk_sizes.setdefault(key, []).append(value)
            elif chunk_id == b"fmt ":
                if size < 16:
                    raise ValueError("Invalid WAV format chunk")
                raw = reader.read(min(size, 40))
                tag, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", raw)
                # WAVE_FORMAT_EXTENSIBLE identifies IEEE float by its subtype GUID.
                if tag == 0xFFFE and len(raw) >= 40 and raw[24:40] == bytes.fromhex("0300000000001000800000aa00389b71"):
                    tag = 3
                if tag != 3 or bits != 32 or channels < 1 or rate < 1 or align != channels * 4 or byte_rate != rate * align:
                    raise ValueError("Audio processing requires float32 WAV")
                fmt = rate, channels, align
            elif chunk_id == b"data":
                data = offset, size
            if fmt is not None and data is not None:
                break
            reader.seek(offset + size + size % 2)
        if fmt is None or data is None or (rf64 and data_size_64 is None):
            raise ValueError("WAV is missing format, data, or RF64 sizes")
        rate, channels, align = fmt
        offset, size = data
        if size % align:
            raise ValueError("WAV data ends inside a sample frame")
        samples = (np.memmap(reader, dtype="<f4", mode="r", offset=offset, shape=(size // align, channels))
                   if size else np.empty((0, channels), dtype=np.float32))
        try:
            yield rate, samples
        finally:
            # Windows cannot delete a file while its memory mapping is open.
            if getattr(samples, "_mmap", None) is not None:
                samples._mmap.close()


def measure_peaks(path, *, cancel_event=None, block_size=BLOCK_SAMPLES):
    sample_peak, oversampled_peak = 0.0, 0.0
    with mapped_float_wav(path) as (rate, samples):
        for start in range(0, len(samples), block_size):
            check_cancel(cancel_event)
            end = min(start + block_size, len(samples))
            lo, hi = max(0, start - PEAK_OVERLAP), min(len(samples), end + PEAK_OVERLAP)
            block = np.asarray(samples[lo:hi], dtype=np.float64)
            if not np.isfinite(block).all():
                raise ValueError("Audio contains non-finite samples")
            sample_peak = max(sample_peak, float(np.max(np.abs(block), initial=0)))
            up = resample_poly(block, 4, 1, axis=0, window=("kaiser", 10.0))
            central = up[(start - lo) * 4:(end - lo) * 4]
            oversampled_peak = max(oversampled_peak, float(np.max(np.abs(central), initial=0)))
        return {"sample_rate": rate, "sample_count": len(samples), "sample_peak": sample_peak,
                "oversampled_peak": oversampled_peak, "peak": max(sample_peak, oversampled_peak)}


def headroom_gain(peak):
    return min(1.0, HEADROOM / peak) if peak > 0 else 1.0


def render_float_file(source, target, *, rate=None, sample_count=None, gain=1.0, cancel_event=None, timeout=90):
    rate = int(rate or media.probe_audio_metadata(source)["sample_rate"])
    filters = [resample_filter(rate)]
    if sample_count is not None:
        filters += [f"apad=whole_len={sample_count}", f"atrim=end_sample={sample_count}"]
    if gain != 1.0:
        filters.append(f"volume={gain:.17g}:precision=float")
    cmd = [media.get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
           "-map", "0:a:0", "-vn", "-af", ",".join(filters), "-c:a", "pcm_f32le", "-rf64", "auto", str(target)]
    media.run_ffmpeg_command(cmd, timeout=timeout, cancel_event=cancel_event)


def delivery_precision(codec, bits=None):
    if codec == "aac":
        return None
    return int(bits or (16 if codec in {"pcm_s16le", "flac"} else 24))


@contextmanager
def prepared_audio(source, audio_args, *, duration=None, cancel_event=None, timeout=90, work_dir=None):
    """Prepare/verify audio before video work; caller owns copying the float sidecar.

    All retries start at the same unmodified float mix. The encoded stream is then
    muxed with copy, so there is no unverified second delivery encode.
    """
    check_cancel(cancel_event)
    media.require_audio_ffmpeg(media.get_ffmpeg_path())
    codec, bitrate = media._audio_codec_bitrate_from_args(audio_args)
    if codec not in {"aac", "alac", "pcm_s16le", "pcm_s24le", "flac"}:
        raise ValueError(f"Unsupported audio codec: {codec}")
    bits = None
    if "-bits_per_raw_sample" in audio_args:
        bits = int(audio_args[audio_args.index("-bits_per_raw_sample") + 1])
    bits = delivery_precision(codec, bits)
    native_rate = int(media.probe_audio_metadata(source)["sample_rate"])
    rate = native_rate
    # AAC does not represent 96 kHz's higher neighbours. Only the delivery is
    # resampled when its codec requires it; supplied AUDIO and native files remain intact.
    if codec == "aac":
        supported = (7350, 8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000)
        rate = min(supported, key=lambda candidate: abs(candidate - native_rate))
    count = None
    if duration is not None:
        exact_count = Fraction(str(duration)) * rate
        count = (2 * exact_count.numerator + exact_count.denominator) // (2 * exact_count.denominator)
    extension = {"aac": ".m4a", "alac": ".m4a", "pcm_s16le": ".wav", "pcm_s24le": ".wav", "flac": ".flac"}[codec]
    with tempfile.TemporaryDirectory(prefix="sonder_audio_delivery_", dir=work_dir or media.transient_temp_dir()) as directory:
        original = os.path.join(directory, "original.wav")
        attenuated = os.path.join(directory, "prepared.wav")
        encoded = os.path.join(directory, "encoded" + extension)
        decoded = os.path.join(directory, "decoded.wav")
        integer_pcm = os.path.join(directory, "integer.wav")
        check_cancel(cancel_event)
        render_float_file(source, original, rate=rate, sample_count=count, cancel_event=cancel_event, timeout=timeout)
        measured = measure_peaks(original, cancel_event=cancel_event)
        gain = headroom_gain(measured["peak"])
        for attempt in range(3):
            check_cancel(cancel_event)
            # Re-evaluate every retry; original remains immutable even when unity
            # gain initially aliases it and verification later requires attenuation.
            prepared = original if gain == 1.0 else attenuated
            if prepared != original:
                render_float_file(original, prepared, rate=rate, gain=gain, cancel_event=cancel_event, timeout=timeout)
            args = ["-c:a", codec]
            if codec == "aac":
                args += ["-b:a", f"{bitrate or 192}k"]
            else:
                write_integer_delivery(prepared, integer_pcm, bits, cancel_event=cancel_event, dither=measured["peak"] != 0.0)
                fmt = "s16" if bits == 16 else "s32"
                args += ["-sample_fmt", "s32p" if codec == "alac" else fmt,
                         "-bits_per_raw_sample", str(bits)]
            codec_input = integer_pcm if bits else prepared
            cmd = [media.get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-i", codec_input, "-vn", *args, encoded]
            media.run_ffmpeg_command(cmd, timeout=timeout, cancel_event=cancel_event)
            if bits:
                os.remove(integer_pcm)
            render_float_file(encoded, decoded, rate=rate, cancel_event=cancel_event, timeout=timeout)
            verified = measure_peaks(decoded, cancel_event=cancel_event)
            os.remove(decoded)
            if verified["peak"] <= 1.0:
                break
            if attempt == 2:
                raise RuntimeError("Encoded audio still overloads after two corrective attempts")
            gain *= HEADROOM / verified["peak"]
        reduction_db = -20 * math.log10(gain) if gain < 1 else 0.0
        warnings = [f"Audio reduced by {reduction_db:.1f} dB to prevent clipping"] if gain < 1 else []
        result = {"sample_rate": rate, "source_sample_rate": native_rate, "intermediate_precision": "float32",
                  "codec": codec, "delivery_bits": bits, "dither": "triangular" if bits and measured["peak"] != 0.0 else None,
                  "sample_count": measured["sample_count"], "sample_peak": measured["sample_peak"],
                  "oversampled_peak": measured["oversampled_peak"], "measured_peak": measured["peak"],
                  "peak_estimator": "4x polyphase Kaiser FIR, block overlap", "gain": gain,
                  "reduction_db": reduction_db, "decoded_sample_peak": verified["sample_peak"],
                  "decoded_oversampled_peak": verified["oversampled_peak"], "corrective_attempts": attempt,
                  "warnings": warnings}
        yield encoded, prepared, result
