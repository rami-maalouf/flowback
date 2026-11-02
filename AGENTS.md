<!-- exact:begin (exact new writes this block; bun exact.mjs update rewrites it) -->
# exact-cover: an Exact app

`app.contract` is the view, `app.ts` its data module, `app.test.contract` its tests, `app.json`
the manifest: set its `app.name` and `app.id` before the first build. The app uses the exact2
checkout at `../exact2` by path (`EXACT2` overrides it).

This file is already in your instructions. Start from the nearest pattern: `bun exact.mjs
pattern` lists them and `pattern <name>` writes one, tested. ../exact2/docs/start-here.md is the guide,
the only required reading (LLP 1115 D7). `bun exact.mjs contract vocab <name>` answers a
name (none: the card, ../exact2/docs/card.md); `bun exact.mjs docs <heading words>` a guide's
section. Run `bun exact.mjs verify` when the features work, and `verify ios|android` at the end for a
phone app. `bun exact.mjs help` lists the verbs, and your own in app.json `"commands"`.
`bun exact.mjs update` refreshes files. `bun exact.mjs lock` repairs Cargo lock drift.
`bun exact.mjs update --without linux` stops shipping Linux.
`bun ../exact2/scripts/exact.mjs setup --check` names what a native build lacks.
The authoring diary: `bun exact.mjs feedback status` says whether this project keeps one.
Shared data: `exact new <path> --snapback`, or `bun exact.mjs snapback` here.
Install `snapback4@0.4.33` above; ../exact2/snapback4/README.md has setup and server commands.

Generated, so don't edit: `exact.mjs`, `rust-toolchain.toml`, `.cargo/config.toml`,
`Cargo.toml`'s `[patch.crates-io]` and this block.
<!-- exact:end -->

## delivery

- commit and push incrementally after each completed feature or fix; do not wait for a large batch of work.
- the public repository is `rami-maalouf/flowback`.

## visual identity

- flowback follows wisp's turquoise palette. use `app.tamber` tokens for interface colors; keep the player css, native accent catalog, and manifest theme color aligned.
- keep the logo playful but restrained. source artwork and its generation prompt live in `assets/`.
- preserve `com.rami.exact-cover` and existing storage paths when changing the display name.
