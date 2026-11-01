"""CPU integration checks executed by test_spatial_mask_runtime in a clean process."""
import ast
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

repo, comfy = map(Path, sys.argv[1:])
sys.path.insert(0, str(comfy))
sys.argv = ["spatial-mask-tests", "--cpu"]

import torch
import nodes
from comfy.ldm.lightricks.vae.causal_video_autoencoder import VideoVAE
from comfy.ldm.lightricks.vae.na_diffusion_decoder import CausalDiffusionVAE
from comfy.ldm.lightricks.symmetric_patchifier import latent_to_pixel_coords
from comfy.ldm.minimax.vae import MiniMaxH3VideoVAE
from comfy.ldm.minimax.model import FRAME_PER_TOKEN
from comfy_extras.nodes_lt import LTXVAddGuide, LTXVConcatAVLatent, get_noise_mask
from comfy.sampler_helpers import prepare_mask

pkg = types.ModuleType("spatial_test_pack")
pkg.__path__ = [str(repo)]
sys.modules[pkg.__name__] = pkg
bridge = importlib.import_module("spatial_test_pack.nodes.masks_bridge")
compiler = importlib.import_module("spatial_test_pack.nodes.spatial_mask")

# Meta construction exercises native configuration and shape code without weights.
with torch.device("meta"):
    models = {
        "ltx": VideoVAE(version=2),
        "ltx-diffusion": CausalDiffusionVAE(),
        "h3": MiniMaxH3VideoVAE(),
        "h3-sol": MiniMaxH3VideoVAE(noncausal_decoder=True),
        "h3-encoder": MiniMaxH3VideoVAE(decoder=False),
    }


def fixture(kind="ltx", count=17, start=0, end=None):
    h3 = kind.startswith("h3")
    fn = (lambda n: max(1, (n - 5) // 17 * 5 + 2) if n > 1 else 1) if h3 else (lambda n: (n + 7) // 8)
    scale, channels = (16, 24) if h3 else (32, 128)
    scene = types.SimpleNamespace(fps=24)
    project = types.SimpleNamespace(
        fps=24, scenes=[scene], get_scene=lambda _: scene,
        _execution_context={"scene_id": "test", "frame_count": count,
                            "mask_start_frame": start, "mask_end_frame": count if end is None else end,
                            "frame_count_padding": 0, "_render_raster_hw": (2 * scale, 3 * scale)},
    )
    vae = types.SimpleNamespace(first_stage_model=models[kind], latent_channels=channels,
                                downscale_ratio=(fn, scale, scale))
    latent = {"samples": torch.zeros(1, channels, fn(count), 2, 3)}
    spatial = torch.full((count, 2 * scale, 3 * scale), .4)
    return project, vae, latent, spatial


def h3_clip_edges(model, count):
    """Token edges from the encoder's own clip chunking, independent of FRAME_PER_TOKEN.

    encode_temporal splits clip_length frames per clip, front-pads each to the
    temporal stride, repeats the last frame to fill a short clip, then drops
    token_drop tokens from the end of the whole sequence. One frame encodes alone.
    """
    if count == 1:
        return [0, 1]
    ends = []
    for start in range(0, count, model.clip_length):
        real = min(model.clip_length, count - start)
        for k in range(model.tokens_chunk_size):
            last = (k + 1) * model.vae_ratio_t - model.frame_pre_padding
            ends.append(start + min(max(last, 1), real))
    return [0, *ends[:len(ends) - model.token_drop]]


def run(f, **kwargs):
    p, v, l, m = f
    return bridge.SonderMasksBridge().execute(p, video_latent=l, video_vae=v, spatial_mask=m, **kwargs)


class SpatialChecks(unittest.TestCase):
    def test_schema_and_positional_abi(self):
        n = bridge.SonderMasksBridge()
        self.assertEqual(list(n.INPUT_TYPES()["optional"]),
                         ["video_latent", "audio_latent", "video_vae", "audio_vae", "spatial_mask"])
        self.assertEqual(n.RETURN_TYPES, ("FLOAT",) * 4 + ("MASK",) * 2)
        p, v, l, m = fixture()
        old = n.execute(p, True, False, l, None, v, None, "positional-id")
        self.assertIn("positional-id", p._execution_context["masks_bridge_by_node"])
        self.assertEqual(tuple(old[4].shape), (3, 1, 1))
        new = n.execute(p, True, False, l, None, v, None, "spatial-id", spatial_mask=m)
        self.assertEqual(old[:4], new[:4])
        self.assertTrue(torch.equal(old[5], new[5]))
        import json
        json.dumps(p._execution_context)
        self.assertNotIn("spatial", p._execution_context["masks_bridge_by_node"]["positional-id"]["video_latent_mask"])

    def test_native_boundaries_and_pooling(self):
        for kind in models:
            counts = (1, 5, 22, 39, 124) if kind.startswith("h3") else (1, 17, 25)
            for count in counts:
                with self.subTest(kind=kind, count=count):
                    p, v, l, m = fixture(kind, count)
                    t = l["samples"].shape[2]
                    if kind.startswith("h3"):
                        ends = torch.tensor(h3_clip_edges(models[kind], count)[1:])
                        self.assertEqual(len(ends), t)
                    else:
                        coords = torch.zeros(1, 3, t + 1, dtype=torch.int64)
                        coords[0, 0] = torch.arange(t + 1)
                        ends = latent_to_pixel_coords(coords, (8, 32, 32), True)[0, 0, 1:]
                    m[:] = (torch.arange(count) / max(1, count - 1))[:, None, None]
                    out = run((p, v, l, m))[4]
                    expected = (ends - 1) / max(1, count - 1)
                    self.assertTrue(torch.equal(out, expected[:, None, None].expand_as(out)))

    def test_moving_regions_and_pool_before_spatial_resize(self):
        f = fixture()
        m = f[3]
        m.zero_()
        m[0, :, :32] = 1
        m[4, :, 32:64] = .6
        m[8, :, 64:] = .4
        m[9, :, :32] = .3
        expected = torch.tensor([[1., 0., 0.], [0., .6, .4], [.3, 0., 0.]])
        self.assertTrue(torch.equal(run(f)[4], expected[:, None, :].expand(3, 2, 3)))
        # A moving line in one token: max before resizing keeps both positions
        # (64 of 1024 pixels), while resizing each frame first would halve it.
        m.zero_()
        m[2, :32, 0] = 1
        m[3, :32, 1] = 1
        self.assertAlmostEqual(run(f)[4][1, 0, 0].item(), 64 / 1024)

    def test_spatial_coverage_is_a_per_cell_mean(self):
        f = fixture()
        m = f[3]
        # Off-centre coverage counts: a 15-row strip misses every centre pixel.
        m.zero_()
        m[:, :15, :32] = 1
        out = run(f)[4]
        self.assertAlmostEqual(out[1, 0, 0].item(), 15 / 32)
        self.assertEqual(out[1, 1, 0].item(), 0.)
        # A centre dot no longer opens the whole cell.
        m.zero_()
        m[:, 15:17, 15:17] = 1
        self.assertAlmostEqual(run(f)[4][1, 0, 0].item(), 4 / 1024)
        # A thin protect band limits strength rather than vanishing.
        m.fill_(1)
        m[:, 9:23, :] = 0
        out = run(f)[4]
        self.assertTrue(torch.allclose(out[1, 0], torch.full((3,), 18 / 32)))
        self.assertTrue(torch.all(out[1, 1] == 1))
        # Grey stays proportional to coverage times its value.
        m.zero_()
        m[:, :16, :32] = .5
        self.assertAlmostEqual(run(f)[4][1, 0, 0].item(), .25)

    def test_protection_freeze_and_audio(self):
        for kind, count, start, end in [("ltx", 25, 9, 17), ("h3", 39, 5, 22)]:
            f = fixture(kind, count, start, end)
            p, v, l, m = f
            p._execution_context["frame_count_padding"] = 2
            lo, hi = (2, 3) if kind == "ltx" else (2, 7)
            audio_shape = (1, 8, 26, 16) if kind == "ltx" else (1, 32, 2, 65)
            audio = {"samples": torch.zeros(audio_shape)}
            av = types.SimpleNamespace(first_stage_model=types.SimpleNamespace(latents_per_second=25 if kind == "ltx" else 40))
            result = run(f, audio_latent=audio, audio_vae=av)
            expected = torch.zeros_like(result[4])
            expected[lo:hi] = .4
            self.assertTrue(torch.equal(result[4], expected))
            self.assertTrue(torch.any(result[5] != 0))
            self.assertTrue(torch.equal(run(f, edit_video=False)[4], torch.zeros_like(expected)))
            both = run(f, edit_audio=False, audio_latent=audio, audio_vae=av)
            self.assertFalse(torch.any(both[5]))
            self.assertTrue(torch.equal(both[4], expected))
        for value in (0., 1.):
            f = fixture()
            f[3].fill_(value)
            self.assertTrue(torch.all(run(f)[4] == value))
        f = fixture()
        self.assertTrue(torch.all(run((*f[:3], f[3].bool()))[4] == 1))

    def test_upstream_forms_ceilings_and_input_immutability(self):
        shapes = [(2, 3), (3, 2, 3), (3, 1, 2, 3), (1, 1, 3, 1, 1),
                  (1, 128, 3, 2, 3), (1, 1), (1, 1, 3)]
        for shape in shapes:
            f = fixture()
            stored = torch.full(shape, .4)
            f[2]["noise_mask"] = stored
            before = stored.clone()
            spatial_before = f[3].clone()
            out = run(f)[4]
            self.assertTrue(torch.all(out == .4))
            self.assertTrue(torch.equal(stored, before))
            self.assertTrue(torch.equal(f[3], spatial_before))
            self.assertNotEqual(out.data_ptr(), stored.data_ptr())
            self.assertNotEqual(out.data_ptr(), f[3].data_ptr())
        f = fixture()
        f[2]["noise_mask"] = torch.ones(1, 1, 3, 2, 3)
        f[2]["noise_mask"][:, :, 1, :, 0] = 0
        self.assertTrue(torch.all(run(f)[4][1, :, 0] == 0))

    def test_rejections_including_frozen_inputs(self):
        for edit in (True, False):
            for value in (float("nan"), float("inf"), -.1, 1.1):
                f = fixture(start=9)
                f[3][0, 0, 0] = value
                with self.assertRaisesRegex(RuntimeError, "finite"):
                    run(f, edit_video=edit)
            for shape in [(1, 64, 96), (3, 64, 96), (17, 96, 64), (17, 1, 64, 96)]:
                f = fixture()
                with self.assertRaisesRegex(RuntimeError, "expected spatial_mask"):
                    run((*f[:3], torch.zeros(shape)), edit_video=edit)
            f = fixture()
            with self.assertRaisesRegex(RuntimeError, "boolean or floating"):
                run((*f[:3], f[3].long()), edit_video=edit)
            for index in (1, 2):
                f = list(fixture())
                f[index] = None
                with self.assertRaisesRegex(RuntimeError, "requires video_latent"):
                    run(f, edit_video=edit)
        for bad in [(2, 1, 3, 2, 3), (1, 2, 3, 2, 3), (3, 2, 2, 3), (4, 2, 3), (3, 4, 6)]:
            f = fixture()
            f[2]["noise_mask"] = torch.ones(bad)
            with self.assertRaisesRegex(RuntimeError, "cannot represent"):
                run(f)
        f = fixture()
        f[2]["noise_mask"] = torch.ones(1, 128, 3, 2, 3)
        f[2]["noise_mask"][0, 2] = 0
        with self.assertRaisesRegex(RuntimeError, "unequal channel"):
            run(f)
        f = fixture()
        f[2]["noise_mask"] = torch.full((1, 1, 3, 1, 1), float("nan"))
        with self.assertRaisesRegex(RuntimeError, "finite"):
            run(f)

    def test_layout_and_raster_guards(self):
        for shape in [(2, 128, 3, 2, 3), (1, 1, 3, 2, 3), (1, 128, 3, 3, 2)]:
            f = fixture()
            f[2]["samples"] = torch.zeros(shape)
            with self.assertRaises(RuntimeError):
                run(f)
        for kind in ("ltx", "h3"):
            f = fixture(kind, 17 if kind == "ltx" else 22)
            f[1].first_stage_model = types.SimpleNamespace(decode_output_shape=models[kind].decode_output_shape)
            with self.assertRaisesRegex(RuntimeError, "unsupported spatial"):
                run(f)
        f = fixture("h3", 22)
        with torch.device("meta"):
            f[1].first_stage_model = MiniMaxH3VideoVAE(encoder=False)
        with self.assertRaisesRegex(RuntimeError, "video encoder"):
            run(f)
        for start, end in [(2, 9), (0, 3), (0, 10)]:
            with self.assertRaisesRegex(RuntimeError, "off the"):
                run(fixture(start=start, end=end))
        f = fixture()
        f[0]._execution_context.pop("_render_raster_hw")
        with self.assertRaisesRegex(RuntimeError, "Editor render raster"):
            run(f)
        f = fixture()
        f[0]._execution_context["_render_raster_hw"] = (128, 192)
        with self.assertRaisesRegex(RuntimeError, "Editor render raster"):
            run(f)
        with self.assertRaisesRegex(RuntimeError, "token edges"):
            run(fixture(count=18))

    def test_optional_module_absence_and_unwired_parity(self):
        original_import = __import__
        def guarded(name, *args, **kwargs):
            if name.startswith("comfy.ldm.minimax"):
                raise ModuleNotFoundError("H3 deliberately absent", name=name)
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", guarded):
            importlib.reload(compiler)
            importlib.reload(bridge)
            self.assertTrue(torch.all(run(fixture())[4] == .4))
            p, v, l, _ = fixture("h3", 22)
            plain = bridge.SonderMasksBridge().execute(p, video_latent=l, video_vae=v)
            self.assertEqual(tuple(plain[4].shape), (7, 1, 1))

    def test_bounded_full_resolution_work(self):
        f = fixture(count=497)
        f = (*f[:3], f[3].half())
        sizes = []
        aminmax = compiler.torch.aminmax
        def measured(group):
            sizes.append(group.numel())
            return aminmax(group)
        with patch.object(compiler.torch, "aminmax", measured):
            run(f)
        self.assertLessEqual(max(sizes), 8 * 64 * 96)
        self.assertEqual(sum(sizes), f[3].numel())

    def test_h3_native_av_sampler(self):
        f = fixture("h3", 22, 5, 22)
        output = run(f)[4]
        audio = {"samples": torch.zeros(1, 32, 2, 37)}
        am = torch.zeros(1, 2, 37)
        am[:, :, 3:12] = 1
        setter = nodes.SetLatentNoiseMask().set_mask
        joined = LTXVConcatAVLatent.execute(setter(f[2], output)[0], setter(audio, am)[0]).result[0]
        masks = [prepare_mask(x, y.shape, "cpu") for x, y in
                 zip(joined["noise_mask"].unbind(), joined["samples"].unbind())]
        self.assertTrue(torch.equal(masks[0][0, 0], output))
        self.assertTrue(torch.equal(masks[1][0, 0], am[0]))

    def test_ltx_writer_retry_and_guide(self):
        path = comfy / "custom_nodes/ComfyUI-LTXVideo/latents.py"
        if not path.exists():
            self.skipTest("Install ComfyUI-LTXVideo to verify its external video mask writer")
        # Compile the installed class unchanged apart from its registration decorator.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if getattr(n, "name", "") == "LTXVSetVideoLatentNoiseMasks")
        cls.decorator_list = []
        namespace = {"torch": torch}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), "exec"), namespace)
        setter = namespace[cls.name]().set_mask
        for upstream in (False, True):
            f = fixture()
            original = f[2]
            if upstream:
                original["noise_mask"] = torch.full((1, 1, 3, 2, 3), .4)
                original["noise_mask"][:, :, 0] = 0
                writer_branch = nodes.LatentFromBatch().frombatch(original, 0, 1)[0]
                before = original["noise_mask"].clone()
            else:
                writer_branch = original
            for column in (0, 2):
                f[3].zero_()
                f[3][:, :, 32 * column:32 * (column + 1)] = .4
                mask = run(f)[4]
                self.assertEqual(mask[1, 0, column].item(), torch.tensor(.4).item())
                applied = setter(writer_branch, mask)[0]
                cond = [[torch.zeros(1, 2, 8), {}]]
                after = LTXVAddGuide.append_keyframe(
                    cond, cond, 1, applied["samples"], get_noise_mask(applied),
                    torch.zeros(1, 128, 1, 2, 3), 1., (8, 32, 32),
                )[-1]
                self.assertTrue(torch.equal(after[0, 0, :3], mask))
                self.assertFalse(torch.any(after[:, :, 3]))
                if upstream:
                    self.assertTrue(torch.equal(original["noise_mask"], before))
        f = fixture()
        f[2]["noise_mask"] = torch.ones(1, 1, 3, 1, 1)
        mask = run(f)[4]
        with self.assertRaises(RuntimeError):
            setter(f[2], mask)


if __name__ == "__main__":
    # unittest flags are separate from ComfyUI's CPU selection above.
    unittest.main(argv=["spatial-mask-tests"], verbosity=2)
