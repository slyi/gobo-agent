'use strict';

const cp = require('child_process');
const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

const MANIFEST_NAME = 'gobo-tests.json';
const PORT = 9235;

let controller;
let output;
const itemInfo = new Map();

function log(message) {
    if (output) output.appendLine(message);
}

function activate(context) {
    output = vscode.window.createOutputChannel('GoboScript tests');
    log(`activate: code ${vscode.version}, trusted=${vscode.workspace.isTrusted}, ` +
        `folders=${(vscode.workspace.workspaceFolders || []).length}`);
    controller = vscode.tests.createTestController('gobo-agent', 'GoboScript (gobo-agent)');
    context.subscriptions.push(output, controller);

    controller.createRunProfile(
        'Headless', vscode.TestRunProfileKind.Run,
        (request, token) => runHandler(request, token, true), true);
    controller.createRunProfile(
        'Visible', vscode.TestRunProfileKind.Run,
        (request, token) => runHandler(request, token, false), false);

    context.subscriptions.push(
        vscode.commands.registerCommand('gobo-agent.refreshTests', () => refresh()),
        vscode.commands.registerCommand('gobo-agent.showLog', () => output.show(true)),
        vscode.workspace.onDidGrantWorkspaceTrust(() => refresh()),
        vscode.workspace.onDidChangeWorkspaceFolders(() => refresh()));

    const watcher = vscode.workspace.createFileSystemWatcher('**/' + MANIFEST_NAME);
    watcher.onDidCreate(() => refresh());
    watcher.onDidChange(() => refresh());
    watcher.onDidDelete(() => refresh());
    context.subscriptions.push(watcher);

    controller.refreshHandler = () => refresh();
    refresh().catch((error) => log(`refresh failed: ${error && error.stack || error}`));
}

function launcher(root) {
    if (process.platform === 'win32') {
        return {
            command: 'powershell',
            args: ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                   path.join(root, 'tools', 'gsdev.ps1')],
        };
    }
    return { command: path.join(root, 'tools', 'gsdev'), args: [] };
}

function execGsdev(root, args, token) {
    return new Promise((resolve) => {
        const { command, args: base } = launcher(root);
        const full = [command, ...base, ...args].join(' ');
        log(`$ ${full}`);
        const env = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
        const child = cp.spawn(command, [...base, ...args],
                               { cwd: root, env, windowsHide: true });
        let stdout = '';
        let stderr = '';
        child.stdout.on('data', (chunk) => { stdout += chunk; });
        child.stderr.on('data', (chunk) => { stderr += chunk; });
        const disposable = token
            ? token.onCancellationRequested(() => { child.kill(); })
            : undefined;
        const finish = (code) => {
            if (disposable) disposable.dispose();
            if (stderr.trim()) log(`stderr: ${stderr.trim()}`);
            resolve({ code, stdout, stderr, cancelled: !!(token && token.isCancellationRequested) });
        };
        child.on('error', (error) => { stderr += String(error); finish(-1); });
        child.on('close', (code) => finish(code === null ? -1 : code));
    });
}

function parseReport(stdout) {
    for (const line of stdout.split(/\r?\n/).reverse()) {
        const text = line.trim();
        if (!text.startsWith('{')) continue;
        try {
            const data = JSON.parse(text);
            if (data && Array.isArray(data.files)) return data;
        } catch (error) {
            // Not the report line; keep looking.
        }
    }
    return null;
}

function clearItems() {
    if (typeof controller.items.replace === 'function') {
        controller.items.replace([]);
        return;
    }
    const ids = [];
    controller.items.forEach((item) => ids.push(item.id));
    for (const id of ids) controller.items.delete(id);
}

async function refresh() {
    clearItems();
    itemInfo.clear();
    if (!vscode.workspace.isTrusted) {
        log('workspace not trusted; not discovering tests');
        return;
    }
    for (const folder of vscode.workspace.workspaceFolders || []) {
        const root = folder.uri.fsPath;
        const manifest = path.join(root, MANIFEST_NAME);
        const exists = fs.existsSync(manifest);
        log(`folder ${root}: manifest ${exists ? 'found' : 'missing'}`);
        if (!exists) continue;
        const result = await execGsdev(root,
            ['test', '--manifest', manifest, '--list'], undefined);
        log(`--list exit ${result.code}`);
        if (result.code !== 0) {
            log(`[list] ${(result.stderr || result.stdout).trim()}`);
            continue;
        }
        let data;
        try {
            data = JSON.parse(result.stdout.trim());
        } catch (error) {
            log(`[list] invalid JSON: ${error}`);
            continue;
        }
        for (const test of data.tests || []) {
            const id = 'gsdev:' + encodeURIComponent(root) + '#' + encodeURIComponent(test.id);
            const item = controller.createTestItem(id, test.label || test.id,
                                                   vscode.Uri.file(test.session));
            item.canResolveChildren = false;
            itemInfo.set(id, { manifest, root, testId: test.id, session: test.session });
            controller.items.add(item);
            log(`added test ${test.id} (${test.session})`);
        }
    }
    log(`discovery done: ${itemInfo.size} test(s)`);
}

function collectTests(request) {
    if (request.include && request.include.length) return request.include;
    const all = [];
    controller.items.forEach((item) => all.push(item));
    return all;
}

async function runHandler(request, token, headless) {
    const run = controller.createTestRun(request);
    const items = collectTests(request).filter((item) => itemInfo.has(item.id));
    const groups = new Map();
    for (const item of items) {
        const info = itemInfo.get(item.id);
        if (!groups.has(info.manifest)) groups.set(info.manifest, []);
        groups.get(info.manifest).push({ item, info });
    }
    try {
        if (!vscode.workspace.isTrusted) {
            for (const item of items) {
                run.errored(item, new vscode.TestMessage('workspace is not trusted'));
            }
            return;
        }
        for (const [manifest, entries] of groups) {
            const root = entries[0].info.root;
            const args = ['test', '--manifest', manifest, '--json', '--port', String(PORT)];
            if (headless) args.push('--headless');
            for (const entry of entries) run.enqueued(entry.item);
            for (const entry of entries) run.started(entry.item);
            const result = await execGsdev(root, args, token);
            if (result.cancelled) {
                for (const entry of entries) {
                    run.appendOutput('run cancelled\r\n', undefined, entry.item);
                    run.failed(entry.item, new vscode.TestMessage('run cancelled'));
                }
                await execGsdev(root, ['close', '--port', String(PORT)], undefined);
                continue;
            }
            const report = parseReport(result.stdout);
            if (!report) {
                const detail = (result.stderr || '').trim();
                const message = new vscode.TestMessage(
                    `gobo-agent test failed (exit ${result.code})${detail ? '\n' + detail : ''}`);
                for (const entry of entries) {
                    run.appendOutput((detail || `exit ${result.code}`) + '\r\n',
                                     undefined, entry.item);
                    run.errored(entry.item, message);
                }
                continue;
            }
            const files = new Map(report.files.map((file) => [file.id, file]));
            for (const entry of entries) {
                const file = files.get(entry.info.testId);
                if (!file) {
                    run.appendOutput('no result reported\r\n', undefined, entry.item);
                    run.errored(entry.item, new vscode.TestMessage('no result reported'));
                    continue;
                }
                const asserts = file.asserts || [];
                const output = asserts
                    .map((assert) => `[${assert.ok ? 'ok' : 'FAIL'}] ${assert.msg}`)
                    .concat(file.errors ? [`${file.errors} error(s)`] : []);
                if (file.bundle) output.push(`bundle: ${file.bundle}`);
                // VS Code renders appendOutput line-by-line and expects \r\n
                // separators; \n or a per-test Location leads to stray indentation.
                run.appendOutput(output.join('\r\n') + '\r\n', undefined, entry.item);
                const problems = asserts
                    .filter((assert) => !assert.ok).map((assert) => assert.msg);
                if (file.errors) problems.push(`${file.errors} error(s)`);
                if (problems.length) {
                    run.failed(entry.item, new vscode.TestMessage(problems.join('\n')));
                } else {
                    run.passed(entry.item);
                }
            }
        }
    } finally {
        run.end();
    }
}

function deactivate() {
    if (controller) controller.dispose();
}

module.exports = { activate, deactivate };
