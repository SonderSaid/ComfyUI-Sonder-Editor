"""ComfyUI's validation refusal in the Sonder App's words (server/validation_refusal.py)."""

from server import validation_refusal as vr

PROMPT = {
    "12": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_2s"}, "_meta": {"title": "Sampler Select"}},
    "20": {"class_type": "EmptyLatent", "inputs": {"width": 7}, "_meta": {"title": "Latent"}},
    "30": {"class_type": "VAEDecode", "inputs": {}},
    "40": {"class_type": "Broken", "inputs": {}, "_meta": {"title": "Odd Loader"}},
}

VIEW = {"bindings": [
    {"label": "Sampler", "targets": [{"path": "12", "input": "sampler_name"}]},
    {"label": "Hidden width", "hidden": True, "targets": [{"path": "20", "input": "width"}]},
]}


def _not_in_list(value="res_2s", name="sampler_name", config=None):
    return {"type": "value_not_in_list", "message": "Value not in list",
            "details": f"{name}: '{value}' not in ['euler']",
            "extra_info": {"input_name": name, "input_config": config, "received_value": value}}


# What ComfyUI's /prompt answers (execution.py validate_inputs), as the proxy
# forwards it in its 422 details.
NODE_ERRORS = {
    "12": {"errors": [_not_in_list()], "dependent_outputs": ["9"], "class_type": "KSamplerSelect"},
    "20": {"errors": [{"type": "value_smaller_than_min", "message": "Value 7 smaller than min of 16", "details": "width",
                       "extra_info": {"input_name": "width", "input_config": ["INT", {"min": 16, "max": 8192}],
                                      "received_value": 7}}],
           "dependent_outputs": ["9"], "class_type": "EmptyLatent"},
    "30": {"errors": [{"type": "required_input_missing", "message": "Required input is missing", "details": "vae",
                       "extra_info": {"input_name": "vae"}}], "dependent_outputs": ["9"], "class_type": "VAEDecode"},
}


def test_each_reason_names_the_setting_in_the_users_terms():
    assert vr.describe_node_errors(NODE_ERRORS, PROMPT, VIEW) == [
        "“Sampler”: “res_2s” isn't one of its choices on this ComfyUI. A node pack or model file may be missing.",
        # A hidden Control is not what the user sees: the node's title and input are.
        "“Latent · width”: 7 is below its minimum, 16.",
        "“VAEDecode” is missing its vae input.",
    ]


def test_without_a_view_or_a_title_it_falls_back_to_the_node():
    lines = vr.describe_node_errors({"12": NODE_ERRORS["12"], "77": {"errors": [_not_in_list()], "class_type": "Gone"}},
                                    {"12": PROMPT["12"]})
    assert lines == ["“Sampler Select · sampler_name”: “res_2s” isn't one of its choices on this ComfyUI. "
                     "A node pack or model file may be missing.",
                     "“Gone · sampler_name”: “res_2s” isn't one of its choices on this ComfyUI. "
                     "A node pack or model file may be missing."]


def test_a_bound_comfyui_did_not_send_uses_its_message():
    above = {"20": {"errors": [{"type": "value_bigger_than_max", "message": "Value 9000 bigger than max of 8192",
                                "extra_info": {"input_name": "width", "input_config": None, "received_value": 9000}}]}}
    assert vr.describe_node_errors(above, PROMPT) == ["“Latent · width”: Value 9000 bigger than max of 8192"]
    above["20"]["errors"][0]["extra_info"]["input_config"] = ["INT", {"max": 8192}]
    assert vr.describe_node_errors(above, PROMPT) == ["“Latent · width”: 9000 is above its maximum, 8192."]


def test_exception_text_details_and_tracebacks_never_reach_the_lines():
    secret = "C:\\Users\\someone\\ComfyUI\\custom_nodes\\x.py"
    errors = {"40": {"errors": [
        {"type": "exception_during_inner_validation", "message": "Exception when validating inner node",
         "details": f"FileNotFoundError: {secret}",
         "extra_info": {"input_name": "ckpt", "exception_message": secret, "traceback": [f'  File "{secret}"']}},
        # A node's own validator: ComfyUI's fixed message, the node's reason in details.
        {"type": "custom_validation_failed", "message": "Custom validation failed for node", "details": f"image - {secret}",
         "extra_info": {"input_name": "image"}},
    ]}}
    lines = vr.describe_node_errors(errors, PROMPT)
    assert lines == ["ComfyUI couldn't check “Odd Loader”. The ComfyUI console has the details.",
                     "“Odd Loader” failed its own check. The ComfyUI console has the details."]
    assert not any("Users" in line or "Error" in line for line in lines)


def test_a_failed_custom_validator_is_one_line_however_many_inputs_it_saw():
    # A **kwargs validator gets every input, and ComfyUI files one error per input.
    repeated = {"40": {"errors": [{"type": "custom_validation_failed", "message": "Custom validation failed for node",
                                   "details": f"{name} - bad", "extra_info": {"input_name": name}}
                                  for name in ("video", "a", "b", "c", "d", "e", "f", "g")]},
                "12": {"errors": [_not_in_list()]}}
    assert vr.describe_node_errors(repeated, PROMPT, VIEW) == [
        "“Odd Loader” failed its own check. The ComfyUI console has the details.",
        "“Sampler”: “res_2s” isn't one of its choices on this ComfyUI. A node pack or model file may be missing.",
    ]


def test_values_are_cleaned_and_capped():
    tricky = "res\u202e2s\x07\u200f\u200b\ufeff\n" + "x" * 300
    line = vr.describe_node_errors({"12": {"errors": [_not_in_list(tricky)]}}, PROMPT)[0]
    assert not any(ch in line for ch in "\u202e\x07\u200f\u200b\ufeff\n")
    quoted = line.split("“")[2].split("”")[0]
    assert len(quoted) == vr.MAX_VALUE_CHARS and quoted.endswith("…")
    long_title = {"12": {**PROMPT["12"], "_meta": {"title": "T" * 400}}}
    # Each value is capped; a line of several long values is capped as a whole.
    line = vr.describe_node_errors({"12": {"errors": [{"type": "x", "message": "m" * 400,
                                                       "extra_info": {"input_name": "i" * 400}}]}}, long_title)[0]
    assert len(line) == vr.MAX_LINE_CHARS and line.endswith("…")
    # A multiselect's list reads as its values.
    assert "“a, b”" in vr.describe_node_errors({"12": {"errors": [_not_in_list(["a", "b"])]}}, PROMPT)[0]


def test_lines_are_deduped_and_limited():
    many = {str(i): {"errors": [_not_in_list(f"v{i}")], "class_type": "N"} for i in range(9)}
    many["0"]["errors"].append(_not_in_list("v0"))  # the same line twice
    lines = vr.describe_node_errors(many, {})
    assert len(lines) == vr.MAX_LINES + 1
    assert lines[-1] == "+3 more. The ComfyUI console lists every one."
    assert len(set(lines)) == len(lines)


# A /prompt refusal captured from ComfyUI 0.45.0 on the test install (2026-11-19).
CAPTURED_PROMPT = {
    "1": {"class_type": "EmptyImage", "inputs": {"width": 0, "height": 64, "batch_size": 1, "color": 0}, "_meta": {"title": "Blank"}},
    "2": {"class_type": "ImageScale", "inputs": {"image": ["1", 0], "upscale_method": "res_2s", "width": 64, "height": 64, "crop": "disabled"}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}},
}
CAPTURED_NODE_ERRORS = {
    "1": {"errors": [{"type": "value_smaller_than_min", "message": "Value 0 smaller than min of 1", "details": "width",
                      "extra_info": {"input_name": "width", "input_config": ["INT", {"default": 512, "min": 1, "max": 16384, "step": 1}],
                                     "received_value": 0}}], "dependent_outputs": ["3"], "class_type": "EmptyImage"},
    "2": {"errors": [{"type": "value_not_in_list", "message": "Value not in list",
                      "details": "upscale_method: 'res_2s' not in ['nearest-exact', 'bilinear', 'area', 'bicubic', 'lanczos']",
                      "extra_info": {"input_name": "upscale_method",
                                     "input_config": [["nearest-exact", "bilinear", "area", "bicubic", "lanczos"], {}],
                                     "received_value": "res_2s"}}], "dependent_outputs": ["3"], "class_type": "ImageScale"},
    "3": {"errors": [{"type": "required_input_missing", "message": "Required input is missing", "details": "filename_prefix",
                      "extra_info": {"input_name": "filename_prefix"}}], "dependent_outputs": ["3"], "class_type": "SaveImage"},
}


def test_a_captured_comfyui_refusal_reads_in_the_users_terms():
    assert vr.describe_node_errors(CAPTURED_NODE_ERRORS, CAPTURED_PROMPT) == [
        "“Blank · width”: 0 is below its minimum, 1.",
        "“ImageScale · upscale_method”: “res_2s” isn't one of its choices on this ComfyUI. A node pack or model file may be missing.",
        "“SaveImage” is missing its filename_prefix input.",
    ]


def test_unexpected_shapes_are_skipped_never_raised():
    for bad in (None, [], "x", {"1": None}, {"1": {"errors": "x"}}, {"1": {"errors": [None, 3, {}]}},
                {"1": {"errors": [{"type": "value_not_in_list", "extra_info": "x"}]}}):
        assert vr.describe_node_errors(bad, PROMPT, VIEW) == []
    assert vr.describe_node_errors(NODE_ERRORS, None, {"bindings": [None, {"label": 3}]})
