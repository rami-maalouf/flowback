<!-- exact:begin (exact new writes this block; bun exact.mjs update rewrites it) -->
# exact-cover: an Exact app

`app.contract` is the view, `app.ts` its data module, `app.test.contract` its tests, `app.json`
the manifest: set its `app.name` and `app.id` before the first build. The app uses the exact2
checkout at `../expo-exact` by path (`EXACT2` overrides it).

This file is already in your instructions. Start from the nearest pattern: `bun exact.mjs
pattern` lists them and `pattern <name>` writes one, tested. ../expo-exact/docs/start-here-site.md (a site) or
../expo-exact/docs/start-here.md is the only required reading. `bun exact.mjs contract vocab <name>` answers a
name (none: the card); `bun exact.mjs docs <heading words>` a guide's
section. Run `bun exact.mjs verify` when the features work, and `verify ios|android` at the end for a
phone app. When an iPhone app is ready for the person to try, run `bun exact.mjs share` and give
them the link it prints; run it again after each change they should see. `bun exact.mjs help` lists the verbs, and your own in app.json `"commands"`.
`bun exact.mjs update` refreshes files. `bun exact.mjs lock` repairs Cargo lock drift.
`bun exact.mjs update --without linux` stops shipping Linux.
`bun ../expo-exact/scripts/exact.mjs setup --check` names what a native build lacks.
Shared data: `exact new <path> --snapback`, or `bun exact.mjs snapback` here.
Install `snapback4@0.4.33` above; ../expo-exact/snapback4/README.md has setup and server commands.

Generated, so don't edit: `exact.mjs`, `rust-toolchain.toml`, `.cargo/config.toml`,
`Cargo.toml`'s `[patch.crates-io]` and this block.

## Feedback (required)

When a doc was wrong or missing, a workaround was needed, something took several tries or a
command confused you, record it: `bun exact.mjs feedback note "<what happened>"`. It attaches
exact2's commit and the last exit code, never output or code. `bun exact.mjs feedback status` says
whether notes are sent and whether to keep a diary; under `never`, skip all of this.
<!-- exact:end -->

## delivery

- commit and push incrementally after each completed feature or fix; do not wait for a large batch of work.
- the public repository is `rami-maalouf/flowback`.

## visual identity

- flowback follows wisp's turquoise palette. use `app.tamber` tokens for interface colors; keep the player css, native accent catalog, and manifest theme color aligned.
- keep the logo playful but restrained. source artwork and its generation prompt live in `assets/`.
- preserve `com.rami.exact-cover` and existing storage paths when changing the display name.
