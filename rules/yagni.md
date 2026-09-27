---
trigger: model_decision
description: "Apply when planning, writing, fixing, refactoring or reviewing code, or choosing a dependency: the YAGNI ladder, root-cause fixes, when not to simplify, and the over-engineering review tags."
---

# YAGNI

Do the simplest thing that actually works. This applies to every code task: write, refactor, fix, review, design, pick a dependency.

## Ladder

Understand the problem first. Then climb, and stop at the first rung that holds:

1. Does the need exist at all? If it is speculative, skip it and say so in one line.
2. Is it already in this repo? Reuse the helper, util, type or pattern. Re-implementing what lives a few files over is the top defect.
3. Does the standard library do it? Use it.
4. Does the platform cover it? `<input type="date">` over a picker library, CSS over JS, a database constraint over app code.
5. Does an installed dependency solve it? Use it. Never add a dependency for what a few lines do.
6. Can it be one line? Make it one line.
7. Only then write the minimum code that works.

When two rungs work, take the higher one. The ladder shortens the solution, never the reading: the smallest change in the wrong place is a second bug.

A bug fix fixes the root cause, not the symptom the report names. Search every caller before you edit a shared function. One guard in the shared function is a smaller diff than one guard per caller, and patching only the path the ticket names leaves sibling callers broken.

## Rules

- No unrequested abstraction: no interface for one implementation, no factory for one product, no config for a value that never changes.
- No boilerplate or scaffolding "for later".
- Deletion over addition. Boring over clever.
- Fewest files. Once the problem is understood, the shortest working diff wins.
- For a complex request, ship the simple version and say what it leaves out: "Did X; Y covers it. Need full X? Say so."
- Between two standard-library options of the same size, take the one that is correct on edge cases. Simple means less code, not a flimsier algorithm.
- A deliberate corner with a known ceiling (global lock, O(n²) scan, naive heuristic) gets one `ceiling:` comment naming the limit and the upgrade trigger, for example `# ceiling: global lock, per-account locks if throughput matters`.

## Output

Code first, then at most three short lines on what you skipped and when to add it. A paragraph defending a simplification is complexity smuggled back as prose. An explanation Daniel asked for is exempt.

## When not to simplify

Never simplify away: validation at a trust boundary, error handling that prevents data loss, security, accessibility basics, anything explicitly requested, or a hardware calibration setting.

Never cut corners on understanding: read the whole flow, and every file the change touches, before picking a rung.

Non-trivial logic (a branch, loop, parser, money or security path) leaves one runnable check behind: an `assert` self-check or one small test, the smallest thing that fails if the logic breaks. No framework, fixture or suite unless asked. A trivial one-liner needs none.

## Over-engineering review

A lens for any diff, branch or repo review, reported next to the correctness findings. It covers complexity only; list findings, never fix them.

Tags:

- `delete:` dead code, unused flexibility, speculative feature. Replacement: nothing.
- `stdlib:` hand-rolled code the standard library ships. Name the function.
- `native:` a dependency or code doing what the platform already does. Name the feature.
- `yagni:` an abstraction with one implementation, config nobody sets, a layer with one caller.
- `shrink:` the same logic in fewer lines. Show the shorter form.

Finding format, the same as `reviewer` with severity `over-engineering`: `path:line: over-engineering: <tag> <what>. <replacement>.` Footer: `net: -<N> lines possible.`, or `Lean already. Ship.` when there is nothing to cut.

For a whole repo, use the same tags and rank the biggest cut first. Look for dependencies the standard library or platform already covers, single-implementation interfaces, one-product factories, wrappers that only delegate, files exporting one thing, dead flags and config. The footer also counts dependencies: `net: -<N> lines, -<M> deps possible.`

Never flag the one self-check: it is the minimum, not bloat.
