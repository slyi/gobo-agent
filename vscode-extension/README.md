# GoboScript Tests (gobo-agent) — VS Code Test Explorer

Optional, maintainer-only extension that shows the `gobo-tests.json` manifest in VS
Code's **Test Explorer**. It only discovers and runs tests through the gobo-agent CLI
(`tools/gsdev`); execution stays in the harness. It has **no npm dependencies** and no
build step, and it is not required — the `Run Tests` task and the terminal do the same
work.

## Prerequisites

- Run gobo-agent setup once (`tools\gsdev.ps1 doctor` should pass), so the launcher can
  resolve Python and the compiler.
- A `gobo-tests.json` (schema 1) in the workspace folder. The repo ships one that runs
  the root `smoke.txt`.
- **Trust the workspace** — VS Code will not run project code in an untrusted workspace,
  and the extension refuses to run tests there.

## Install

A built `gobo-agent-tests-<version>.vsix` is committed in this folder, so you do **not**
need Node to use it.

**From the VS Code UI (no restart needed)**

1. Open the **Extensions** view (`Ctrl+Shift+X`).
2. Click the **⋯** (**Views and More Actions**) button in the **top-right** of the
   Extensions view.
3. Choose **Install from VSIX…**
4. Select `vscode-extension/gobo-agent-tests-<version>.vsix`. It activates in the current
   window — no restart is needed; you can dismiss any Reload prompt.

**From the terminal**

```sh
code --install-extension vscode-extension/gobo-agent-tests-<version>.vsix
```

`code --install-extension` registers the extension for **new** windows; an already-open
window picks it up after **Developer: Reload Window** (a full restart isn't needed).

Either way, open the repo folder afterwards; the extension activates because
`gobo-tests.json` exists. Open the **Testing** view (beaker icon in the activity bar),
pick **Headless** (default) or **Visible**, and run the test(s) or **Run All Tests**.
If the view is empty, run Command Palette (`Ctrl+Shift+P`) → **GoboScript: Refresh Tests**.

## Rebuilding the VSIX (maintainers)

Plain JavaScript, no dependencies; packaging just bundles the folder and needs Node
(`npx`):

```sh
cd vscode-extension
npx @vscode/vsce package --allow-missing-repository --skip-license
# produces gobo-agent-tests-<version>.vsix — commit it alongside the source
```

Bump `package.json` `version` before repackaging so installs upgrade.

This is the reliable path: Test Explorer runs in your normal window, so there is no
Extension Development Host to set up. (Uninstall with
`code --uninstall-extension gobo-agent.gobo-agent-tests`, or right-click the extension in
the Extensions view → *Uninstall*.)

## Option B — Extension Development Host (F5)

Press **F5** (**Run and Debug** → *Run Extension (gobo-agent tests)*). Depending on VS
Code's window handling the host may open **without a folder**; if so, use the installed
extension above instead — F5 is optional. When it does open with the repo folder, the
**Testing** view lists the tests as above.

Both paths run their own browser on port **9235**, separate from your interactive host
(default **9230**). Don't point a normal run/test at 9235 while the extension is using it.

## What it runs

- Discovery: `test --manifest gobo-tests.json --list` (read-only, no browser).
- Run: `test --manifest gobo-tests.json --json --port 9235` (plus `--headless` for the
  headless profile). Failures come from the structured report — never from stdout
  scraping — and each failure links its `gobo-tests.json` session file. A failing run's
  bundle path is included in the message.

## Troubleshooting

- **F5 opens a window with no folder, or focus jumps back to the original window** — the
  Extension Development Host already inherits the repo folder. Don't *Open Folder* for
  the same folder (VS Code focuses the existing window); close the extra window and press
  F5 again.
- **No tests appear** — check the workspace is trusted, `gobo-tests.json` is at the
  folder root, and setup has run. `--list` errors are written to the **GoboScript tests**
  output channel.
- **Python / `test` errors** — run setup; on Windows it fetches portable Python.
- **"run failed" with a port error** — something else is on 9235; close it with
  `tools\gsdev.ps1 close --port 9235`, or change `PORT` in `extension.js`.
- **Manifest problems** (duplicate ids, missing sessions, path escapes) — validate with
  `tools\gsdev.ps1 test --manifest gobo-tests.json --list`.
