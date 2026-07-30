# Changelog

All notable changes to **ComfyUI-Sonder-Editor** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release section is dated `YYYY-MM-DD`. Add new entries under `[Unreleased]`
as you work; on release, rename that heading to the new version + date and start
a fresh `[Unreleased]` block.

## [Unreleased]

### Fixed
- Post-save asset refreshes now coalesce per browser window and use read-only
  registry reads, avoiding duplicate synchronization scans and summary-request
  abort churn while preserving explicit discovery and repair.

## [0.1.1] - 2026-07-27

### Added
- Added shared frontend project-source resolution for direct wires, typed
  pass-through/reroute chains, and current KJNodes Set/Get scopes.
- README now carries a 90-second overview video and three technique clips —
  Prompt Relay, Guides, and IC-LoRA motion transfer — from the sample project.

### Changed
- Rewrote the Registry and Manager listing description in plain language that
  names what the editor does, replacing the "NLE-style" framing.

### Fixed
- Sonder Editor now skips unused timeline-frame, direct-guide, and audio
  materialization while preserving project, queue, bridge, and metadata
  context.
- Guide **Set Selection In/Out** actions now use the same two-stage manual
  endpoint workflow as toolbar and shortcut entry.
- Animatic video suppression is now ephemeral and no longer overwrites durable
  lane visibility when the mode is toggled or a scene changes.
- Save Bridge restores its serialized target-folder field after workflow load
  without invoking the widget callback or dirtying the graph.
- Bridge asset-arrival notices now compare asset identities per execution
  settlement instead of relying on total counts.

## [0.1.0] - 2026-07-22

### Added
- Initial public release of **Sonder Editor**.
- Project documentation, an example LTX 2.3 workflow, and a downloadable
  showcase project.

### Notes
- Requires ComfyUI and `ffmpeg` (see README). `torch`/`torchaudio` are provided
  by ComfyUI and are intentionally excluded from this pack's requirements.
