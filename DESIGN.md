# Codex HUD · iOS Frosted Glass

User-approved direction: iOS-style translucent frosted glass, 2026-10-27.

Reference: VoltAgent/awesome-design-md, Apple DESIGN.md (vendored in
docs/design-reference/apple.DESIGN.md, upstream blob
0d37e1e5f6b621ccbdc666f6d9c82f2ada26208b).
The upstream document analyses Apple's website; this HUD adapts its neutral
palette, typography hierarchy and restrained blue to a desktop glass material.
It is not an official Apple iOS specification.

## Visual system
- Pearl #F5F5F7, ink #1D1D1F, secondary #63636B, action #007AFF.
- Keep red/off and green/on semantics for existing automation controls.
- Native Windows Composition backdrop provides real desktop blur.
- Translucent neutral white shell, fine light rim, soft upper highlight.
- Separate rounded quota tiles; primary quota has a large left-aligned numeral.
- Segoe UI Variable Display for numbers; Microsoft YaHei UI for Chinese.
- 428 logical pixels wide; 22px outer inset, 18px card inset, 12px spacing.
- Keep text opaque. If blur is unavailable, use a readable near-opaque surface.
- Preserve refresh, pin, tray, settings, records, automation opt-in and shortcuts.

## Acceptance
Run existing tests, verify geometry and fallback rendering, build the EXE,
launch offline demo, inspect pixels and native backdrop diagnostics.
Keep stable tags and the old release recoverable.
