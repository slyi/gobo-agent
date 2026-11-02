# GoboScript code style guide

Use these conventions for GoboScript projects. Keep the approved appearance,
animation timing and native Scratch behavior intact while making code smaller,
clearer and cheaper to run.

## Naming and layout

- Use descriptive `camelCase` for variables, lists, procedures and new sprites:
  `mouthPlanner`, `prepareGaze`, `mouthStrokeScale`.
- Name a sprite for the work it owns. Keep one clear responsibility per sprite.
- Use consistent indentation and spaces around operators in new code. Do not
  reformat large data tables or unrelated code during a functional change.
- Remove unused procedures, declarations, obsolete branches and superseded
  implementations. Preserve supported poses, controls and model-specific paths.
- Keep shared algorithms in one maintained source. An include can share source,
  but its expanded blocks still count in every sprite that includes it.

## Sprite size and abstraction

- Keep each sprite around **2,000 editor blocks** as a readability and
  maintainability budget, not a Scratch hard limit. Count reconstructed non-shadow
  blocks, including inline variable/list reporters. Raw JSON block records omit
  those inline reporters and include shadows, so they are a different metric.
- Document measured exceptions for tightly coupled rendering or sorting paths.
  Report their counts and preserve performance rather than adding per-item handoffs
  simply to satisfy the size target.
- When a sprite exceeds that budget, first remove unused code and factor repeated
  calculations into procedures. Then split at a clear responsibility boundary.
- Prefer separate projection, planning, data/setup and drawing responsibilities
  over arbitrary chunks of equally sized code.
- Keep procedures used together in tight loops together. Do not replace local
  procedure calls with a broadcast for every point, stroke or mesh primitive.
- Make each stage's inputs, outputs and next event clear. Keep one owner for each
  mutable value and avoid running old and replacement planners together.
- Save helper sprites hidden and call `hide;` during their green-flag (`onflag`) handler.

## When to refactor

Refactor when a sprite exceeds the 2,000-block style budget, or when one of these issues
has a concrete cost:

- The same calculation, constant table or algorithm must be fixed in several places.
- One procedure mixes independent jobs such as projection, expression selection,
  drawing, sorting and input handling.
- A feature depends on many unrelated global scratch values or unclear ownership.
- Repeated temporary assignments, list copies, scans or recalculations dominate
  a measured hot path.
- A small feature change requires editing several unrelated sprites, or old and
  replacement implementations can both run in the same frame.
- Complex nesting or implicit dependencies make the execution order hard to follow.

Before changing structure, record the current block counts, representative output
and frame profile. Set a specific result: smaller sprites, one maintained algorithm,
clearer ownership or less measured work. Preserve the approved behavior.

Split at a stage boundary with a small, explicit interface. Keep tightly coupled
loops together and cache reused inputs once on entry. Avoid creating extra shared
state or many small broadcast handoffs just to reduce one sprite's count.

Build and check each bounded step before continuing. Remove superseded code after
the replacement passes its comparisons. A refactor is complete when its supported
cases still work, the size budget is met and matched profiles show no regression.

## Comments and readable procedures

- Give every procedure **one short `//` header immediately above its declaration**.
  The gobo-agent build places this comment first inside the Scratch proc; plain
  `goboscript build` does not accept `//` comments, so build through `gsdev`.
- Explain purpose or a necessary constraint, rather than narrating assignments.
- Add short internal comments for non-obvious geometry, coordinate systems,
  model-specific offsets, caching assumptions or rendering limitations.
- Keep extra comment blocks out of hot loops: retained `//` comments also dispatch
  as blocks. Use `#` for compile-time notes that need not appear in the editor.
- Credit borrowed algorithms or assets beside the relevant source.

```text
// Queue one eyelid segment in the current projected eye basis.
proc queueLidSegment x0, y0, x1, y1, width {
    queueLine $x0, $y0, $x1, $y1, $width;
}
```

## Variables, lists and procedure inputs

- Make variables and lists sprite-local by default. Reserve globals for actual
  shared interfaces and explicitly shared native monitors.
- Keep public global display names consistent with the project's chosen prefix.
  Keep implementation scratch values local and out of the monitor list.
- Read another sprite's scalar with `"spriteName"."variableName"`. Cache it locally
  once at a planning boundary when several calculations reuse it.
- Pass a single-use expression directly into a procedure input. Avoid assigning
  a temporary variable or list item merely to pass it into the next call.
- Retain a temporary when it avoids repeated expensive work or makes a complex
  calculation substantially easier to read. Do not remove useful caches.
- Prefer named procedure inputs to a collection of global scratch arguments.
- Use compound assignments where they reduce work. In the current compiler,
  `x += value` lowers to Scratch's change-variable block; `x -= value` still uses a
  set and subtraction. Consider `x += -value` in measured hot paths.
- Avoid repeated list writes, full-list copies, list searches and membership
  checks inside frame loops. Use indexed access and bounded, known strides.

```text
# Pass the compound expression directly; do not create throwaway endpoint vars.
queueLine cx - hx, cy - hy, cx + hx, cy + hy, diameter;

# Cache an external value when the current stage uses it repeatedly.
cameraYaw = "camera"."yaw";
```

## Performance targets

- Aim for **100,000 executed Scratch opcodes per displayed frame or fewer** and
  **fewer than 1,000 pen/stamp draws per frame** as code-style targets for a
  readable, maintainable frame path. These are not Scratch limits; a project may
  justify a different budget. Report current results and any remaining gaps.
- Treat these as measured optimization targets, not limits enforced by the VM. A
  small stored block count does not establish runtime cost.
- Use the built-in gsdev/gsbridge profiler for frame opcodes and draws. Do not
  estimate cost from opcode names or add custom counters to measure VM dispatches.
- Compare matched builds, models, source frames, detail levels and browser modes.
  Check representative frames with the same moving warmup, plus affected views.
- Keep profiling off for FPS measurements. Profiler overhead changes timing.
- Use detail levels appropriate to projected size. Reserve fine cosmetic details
  for views where they are visible.
- Calculate reusable ellipse, lens, lighting and camera terms once. Reuse them
  only when their geometry and visibility assumptions remain valid.
- Decode compressed animation/lookup tables once during setup. Do not parse
  packed text or rebuild static tables during frame rendering.
- Use the existing model sort adapters and generic sorter. Avoid extra sort passes,
  polling loops, `broadcast_and_wait` or `wait 0` in new frame paths unless needed
  for a demonstrated scheduling dependency.
- Batch prepared commands and draw them in the established order. Keep new sprite
  handoffs at stage boundaries, with no per-primitive broadcasts.

## Large data and load-time decoding

Use a compact, reproducible representation for large static geometry, animation or
lookup tables. Avoid embedding unnecessary full-precision floats or maintaining
several copies of the same payload.

- Choose a quantization scale from the data's units and acceptable output error.
  Convert values to integers with a documented rounding rule. Use enough precision
  to preserve the result; do not choose decimal places solely to shrink the file.
- Delta-encode successive integer values. For interleaved channels, track the
  previous value separately for each channel using a documented stride; unrelated
  channels should not become each other's delta base.
- Add run-length encoding when repeated deltas, especially zeros, make it smaller.
  Keep signed values and run delimiters unambiguous. Measure the encoded result;
  compression can add overhead to short or irregular tables.
- Record the format/version, quantization scale, stride and expected decoded count.
  Keep the canonical input and encoder so the packed payload can be regenerated.
  Do not hand-edit packed strings.
- Decode once during setup/load into the owning sprite's indexed lists. Let setup
  finish before frame planners consume the data. Normal frame rendering should
  read the expanded values directly.
- Guard initialization so another green flag does not append duplicate data or
  decode it again unnecessarily. Make an intentional reset explicit when the
  source payload changes.
- Parse with a forward cursor and integer accumulator. Avoid repeatedly slicing
  away the front of a large string, rescanning it or searching a growing list.
- Release decoder-only strings, temporary lists and scratch storage after setup
  when they are no longer needed. Keep decoded tables local unless another sprite
  has a genuine shared-data requirement.
- Validate encoder/decoder round trips offline: count, channel order, signed deltas,
  repeated runs, boundary values and maximum quantization error. Then check native
  VM decoded values and representative rendered output against the original.
- Measure startup cost, JSON/SB3 bytes, expanded list sizes and runtime memory too.
  A smaller packed file can still expand into a large working set. Use preflight
  checks and verify that repeated initialization keeps list lengths stable.
- Confirm with the frame profiler that decoder procedures are absent from normal
  frame execution. Load-time savings must not become per-frame parsing work.

## Monitors and controls

- Use **Scratch-native variable/list monitors and sliders** for frame number, FPS,
  draw count, model statistics and debugging controls.
- Do not draw replacement HUD counters, labels or sliders with pen/stamps or
  create costume UI for information Scratch monitors already provide.
- Hide secondary statistics by default and provide an optional native toggle when
  useful. Keep related statistics consistent when shown or hidden.
- Show controls only in modes or contexts where they apply.
- Keep controls responsive on a held frame through the existing redraw mechanism.

## Building and checking changes

- Build through **gobo-agent commands**, using the project's configured launcher,
  so retained comments and build mode are handled consistently. For example,
  `tools\gsdev.ps1 build --mode release` on Windows, or
  `python tools/gsdev.py build --mode release` through the portable Python CLI.
  Run the project's required asset, naming and preflight steps too.
- Keep source and the canonical release SB3 in sync. Avoid unnecessary duplicate
  output files or obsolete aliases.
- Review per-sprite editor block counts after a refactor. Check frame opcodes and draw
  counts before claiming a performance improvement.
- For rendering changes, compare native prepared geometry and raster output at
  relevant poses, profile angles and animation cues. Preserve approved timing,
  occlusion and surface attachment.
- Check affected models, rendering modes, detail levels, monitor toggles and
  repeated green-flag initialization where applicable.
- Reuse one owned headless gobo-agent session for related checks, then close it.
- Keep validation reports tied to the exact SB3 hash they tested. Store debug
  captures outside active sprite sources and leave archived versions inactive.

## Understand the developer's intent

Distinguish a requested outcome or visual preference from a proposed technical
explanation. Preserve the outcome, but independently verify measurements,
comparisons and assumptions. Reproduce the reported scenario, confirm the build
and runtime state, and compare like for like before assigning a cause. Explain
inconsistent evidence plainly; a confident report is still a hypothesis to test.
See [Working with the developer](../AGENTS.md#working-with-the-developer) for the
collaboration workflow.

## JSON packing

Packing can remove redundant shadows, unused definitions and editor metadata, or
shorten identifiers. Report JSON bytes, SB3 bytes and editor blocks separately:
fewer stored bytes do not establish fewer frame opcodes or a smaller editor graph.
Check name-based lookups such as `"spriteName"."variableName"` before identifier
minification; renaming a definition without its sensing-property string breaks the
reference. Preserve the maintained source and validate the packed release in the
native VM before making packing a build step.

## Verification

Build and verify through the project's gobo-agent launcher. For the owned warm
session, exact reproduction, session assertions, profiling, screenshot checks and
performance-comparison method, see
[Debugging workflow in AGENTS.md](../AGENTS.md#debugging-workflow).

Keep profiling off for final FPS and visual captures. Compare matched builds and
scenarios; a small stored block count does not establish runtime cost.
