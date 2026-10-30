"""Audited image-input capability: the single source for QA-evidence validation and delivery.

Image input requires BOTH an adapter transport and a model that accepts images. Lookup is exact
on the resolved model ID: no prefix/family matching, no fallback, no runtime catalog read.

Audit evidence (local, no network): Codex CLI 0.163.0 `exec --help` (`-i, --image <FILE>...`)
and its bundled model catalog (`input_modalities: [text, image]`); Claude Code 2.1.299 `--help`
(images only via `--input-format stream-json`, which also requires stream-json output and so
changes the audited result envelope; not supported by ClaudeStageExecutor).
"""
from types import MappingProxyType

IMAGE_INPUT_MODELS = MappingProxyType({
    'codex': frozenset({
        'gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna',
        'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna',
    }),
    'claude': frozenset(),  # No audited image transport in the Claude adapter.
})


def supports_image_input(provider, resolved_model):
    return resolved_model in IMAGE_INPUT_MODELS.get(provider, frozenset())
