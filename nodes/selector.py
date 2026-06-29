"""Small selector utilities for Sonder workflows."""

from __future__ import annotations

from collections import Counter


DEFAULT_CHOICES = ("Option 1", "Option 2")


def _parse_choice_list(choice_list) -> list[str]:
    return [
        line.strip()
        for line in str(choice_list or "").splitlines()
        if line.strip()
    ]


def _validate_choice(choice, choice_list) -> bool | str:
    choices = _parse_choice_list(choice_list)
    if not choices:
        return "Sonder Selector requires at least one non-empty choice."

    duplicates = [value for value, count in Counter(choices).items() if count > 1]
    if duplicates:
        return f"Sonder Selector choice list contains duplicate label: {duplicates[0]}"

    selected = str(choice or "").strip()
    if selected not in choices:
        return f"Sonder Selector choice '{selected}' is not in the choice list."
    return True


class SonderSelector:
    """Select a string and zero-based index from a newline-delimited list."""

    # Dynamic combo behavior adapted from wakaura-asaho/comfyui-dynamic-selector.

    CATEGORY = "Sonder/Logic"
    RETURN_TYPES = ("STRING", "INT")
    RETURN_NAMES = ("selection", "index")
    OUTPUT_TOOLTIPS = (
        "Selected label from the newline-delimited choice list.",
        "Zero-based index of the selected label.",
    )
    FUNCTION = "execute"
    DESCRIPTION = "Selects one label from a newline-delimited list and outputs its text plus zero-based index."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "choice": (list(DEFAULT_CHOICES), {
                    "default": DEFAULT_CHOICES[0],
                    "tooltip": "Selected label from choice_list.",
                }),
                "choice_list": ("STRING", {
                    "default": "\n".join(DEFAULT_CHOICES),
                    "multiline": True,
                    "tooltip": "One selectable label per line. Blank lines are ignored.",
                }),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, choice="", choice_list="", **kwargs):
        return _validate_choice(choice, choice_list)

    def execute(self, choice, choice_list):
        valid = _validate_choice(choice, choice_list)
        if valid is not True:
            raise ValueError(valid)

        choices = _parse_choice_list(choice_list)
        selected = str(choice or "").strip()
        return (selected, choices.index(selected))
