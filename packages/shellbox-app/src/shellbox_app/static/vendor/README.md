# Vendored browser assets

These are BUILT files, committed on purpose. There is no `package.json`, no `node_modules`, and
no build step anywhere in this repository -- ADR-23 declines a JavaScript toolchain, and
vendoring is what makes that possible without also giving up a terminal emulator.

## Why not a CDN

The Databricks Apps edge authenticates the request that fetched `index.html`. A `<script>`
pointing at `unpkg` or `jsdelivr` is an unauthenticated third-party dependency on the load path
of an authenticated page: it decides whether the terminal renders at all, it is a request this
App cannot promise anything about, and it fails closed in exactly the environments that matter
most. `tests/unit/test_static_assets.py` asserts that no file under `static/` names an absolute
URL, so a CDN reference fails `make test`.

## What is here, and where it came from

Fetched 2026-08-04 from `unpkg.com`, which is the mirror this workstation can reach
(`registry.npmjs.org` is not routable from here).

| File | Package | Version | Source path | sha256 |
|---|---|---|---|---|
| `xterm.js` | `@xterm/xterm` | 5.5.0 | `lib/xterm.js` | `1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495` |
| `xterm.css` | `@xterm/xterm` | 5.5.0 | `css/xterm.css` | `ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6` |
| `addon-fit.js` | `@xterm/addon-fit` | 0.10.0 | `lib/addon-fit.js` | `bdaefa370b1bfc42ee88d46fe6072400902a4d4b2d45cd93438dda9b23c97089` |
| `LICENSE` | `@xterm/xterm` | 5.5.0 | `LICENSE` | `b569f629d00f2626a8100df2a1798210535621e42164dfd426a6fe5aac7b0ccd` |

Both bundles are UMD, so they define `window.Terminal` and `window.FitAddon` when loaded as
plain `<script>` tags. That is why `index.html` loads them that way and loads `terminal.js` as a
module: `type="module"` is deferred by definition, so it runs after both.

`tests/unit/test_static_assets.py` checks these hashes on every `make test`. A refresh is
therefore a two-file change -- the asset and the row above -- and the mismatch is what stops an
asset from being swapped without a reader noticing.

## Refreshing

```sh
V=packages/shellbox-app/src/shellbox_app/static/vendor
curl -sSL -o "$V/xterm.js"     https://unpkg.com/@xterm/xterm@5.5.0/lib/xterm.js
curl -sSL -o "$V/xterm.css"    https://unpkg.com/@xterm/xterm@5.5.0/css/xterm.css
curl -sSL -o "$V/addon-fit.js" https://unpkg.com/@xterm/addon-fit@0.10.0/lib/addon-fit.js
curl -sSL -o "$V/LICENSE"      https://unpkg.com/@xterm/xterm@5.5.0/LICENSE
shasum -a 256 "$V"/*
```

Then update the table above with the printed hashes, and read xterm's changelog: this repo has
**no browser test lane**, so nothing here will fail on a behavioural regression in the emulator.
That is ADR-23's second stated cost, and an upgrade is exactly when it is collected.

## Licence

xterm.js is MIT, and its licence is committed beside the bundles as `LICENSE`.
