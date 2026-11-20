'use strict';

const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { forwardsSigint } = require('../bin/devdocs');

const POSIX_ONLY = process.platform === 'win32' && 'uses a POSIX shell script as the interpreter';
const PTY_ONLY = !['darwin', 'linux'].includes(process.platform)
  && 'needs script(1) from macOS or util-linux';

/**
 * Answers the version probe as 3.13; otherwise reports how it ran, then acts
 * on FAKE_ACTION. Its waits end after a few seconds, so a lost signal fails a
 * test instead of hanging it.
 */
const FAKE_PYTHON = `#!/bin/sh
if [ "$1" = "-c" ]; then echo 3.13; exit 0; fi
printf 'args:'; printf ' [%s]' "$@"; echo
echo "PYTHONPATH: \${PYTHONPATH-unset}"
echo "PYTHONDONTWRITEBYTECODE: \${PYTHONDONTWRITEBYTECODE-unset}"
case "$FAKE_ACTION" in
  exit) exit 7 ;;
  die) kill -TERM $$ ;;
  wait)
    trap 'echo "got INT"; exit 43' INT
    trap 'echo "got TERM"; exit 42' TERM
    echo ready
    for _ in 1 2 3 4 5 6 7 8 9 10; do sleep 1 >/dev/null 2>&1 & wait $!; done
    echo "no signal arrived"; exit 99 ;;
  count)
    n=0
    trap 'n=$((n + 1))' INT
    echo ready
    for _ in 1 2 3; do sleep 1 >/dev/null 2>&1 & wait $!; done
    echo "INT x$n"; exit 0 ;;
esac
`;

/** A copy of the package with a stand-in bundle and the fake interpreter. */
function install() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'devdocs-launcher-')));
  for (const file of ['bin/devdocs.js', 'lib/python.js', 'scripts/postinstall.js']) {
    fs.mkdirSync(path.join(dir, path.dirname(file)), { recursive: true });
    fs.copyFileSync(path.join(__dirname, '..', file), path.join(dir, file));
  }
  fs.mkdirSync(path.join(dir, 'python', 'devdocs'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'python', 'devdocs', '__main__.py'), '');
  fs.writeFileSync(path.join(dir, 'fake-python'), FAKE_PYTHON, { mode: 0o755 });
  return dir;
}

/** A test given a fresh copy of the package, removed afterwards; POSIX only unless `skip` says. */
function launcherTest(name, body, options = {}) {
  test(name, { skip: POSIX_ONLY, ...options }, async () => {
    const dir = install();
    try {
      await body(dir);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
}

/** Starts a script of the copy with a minimal environment; `done` resolves when it closes. */
function start(dir, script, args, env, options = {}) {
  const child = spawn(process.execPath, [path.join(dir, script), ...args], {
    env: { PATH: process.env.PATH, DEVDOCS_PYTHON: path.join(dir, 'fake-python'), ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
    ...options,
  });
  const output = { stdout: '', stderr: '' };
  child.stdout.on('data', (chunk) => { output.stdout += chunk; });
  child.stderr.on('data', (chunk) => { output.stderr += chunk; });
  const done = new Promise((resolve, reject) => {
    child.on('error', reject);
    child.on('close', (code, signal) => resolve({ code, signal, ...output }));
  });
  return { child, done };
}

function waitFor(stream, text) {
  return new Promise((resolve) => {
    let seen = '';
    stream.on('data', function check(chunk) {
      seen += chunk;
      if (seen.includes(text)) {
        stream.off('data', check);
        resolve();
      }
    });
  });
}

/** script(1) arguments that run the sh command line `line` on a new pty. */
function onPty(line) {
  return process.platform === 'darwin'
    ? ['-q', '/dev/null', '/bin/sh', '-c', line]
    : ['-q', '-c', line, '/dev/null'];
}

/** Resolves once `check()` holds, polling every 50 ms; rejects after `ms`. */
async function until(check, ms = 10000) {
  for (const end = Date.now() + ms; !check();) {
    if (Date.now() > end) throw new Error('timed out waiting');
    await new Promise((resolve) => { setTimeout(resolve, 50); });
  }
}

launcherTest('runs python -P -m devdocs with the arguments, the bundle first on PYTHONPATH', async (dir) => {
  const env = { FAKE_ACTION: 'exit', PYTHONPATH: '/elsewhere' };
  const result = await start(dir, 'bin/devdocs.js', ['generate', 'a b', '--quiet'], env).done;
  assert.equal(result.code, 7);
  assert.match(result.stdout, /^args: \[-P\] \[-m\] \[devdocs\] \[generate\] \[a b\] \[--quiet\]$/m);
  const pythonPath = [path.join(dir, 'python'), '/elsewhere'].join(path.delimiter);
  assert.ok(result.stdout.includes(`PYTHONPATH: ${pythonPath}\n`));
  assert.match(result.stdout, /^PYTHONDONTWRITEBYTECODE: 1$/m);
});

launcherTest('keeps a PYTHONDONTWRITEBYTECODE the caller set', async (dir) => {
  const env = { FAKE_ACTION: 'exit', PYTHONDONTWRITEBYTECODE: '' };
  const result = await start(dir, 'bin/devdocs.js', [], env).done;
  assert.match(result.stdout, /^PYTHONDONTWRITEBYTECODE: $/m);
  assert.ok(result.stdout.includes(`PYTHONPATH: ${path.join(dir, 'python')}\n`));
});

launcherTest('keeps only absolute inherited PYTHONPATH entries, after the bundle', async (dir) => {
  const inherited = ['', '/one', 'relative', '.', '/two', ''].join(path.delimiter);
  const env = { FAKE_ACTION: 'exit', PYTHONPATH: inherited };
  const result = await start(dir, 'bin/devdocs.js', [], env).done;
  const pythonPath = [path.join(dir, 'python'), '/one', '/two'].join(path.delimiter);
  assert.ok(result.stdout.includes(`PYTHONPATH: ${pythonPath}\n`), result.stdout);
});

launcherTest('refuses a bundle whose path PYTHONPATH cannot hold', async (dir) => {
  const nested = path.join(dir, `a${path.delimiter}b`);
  for (const name of ['bin', 'lib', 'python']) {
    fs.cpSync(path.join(dir, name), path.join(nested, name), { recursive: true });
  }
  const env = { FAKE_ACTION: 'exit', DEVDOCS_PYTHON: path.join(dir, 'fake-python') };
  const result = await start(nested, 'bin/devdocs.js', ['--version'], env).done;
  assert.equal(result.code, 1);
  assert.ok(result.stderr.includes(`cannot run from ${path.join(nested, 'python')}`), result.stderr);
  assert.match(result.stderr, /PYTHONPATH cannot hold a path containing/);
  assert.equal(result.stdout, '');
});

launcherTest('exits with 128 + the signal number when a signal ends the child', async (dir) => {
  const result = await start(dir, 'bin/devdocs.js', [], { FAKE_ACTION: 'die' }).done;
  assert.equal(result.code, 128 + os.constants.signals.SIGTERM);
});

test('passes SIGINT on only without a controlling terminal, and never on Windows', () => {
  const asked = [];
  const terminal = (present) => () => {
    asked.push(present);
    return present;
  };
  assert.equal(forwardsSigint('linux', terminal(false)), true);
  assert.equal(forwardsSigint('darwin', terminal(true)), false);
  assert.equal(forwardsSigint('win32', terminal(false)), false);
  assert.deepEqual(asked, [false, true], 'Windows needs no terminal check');
});

launcherTest('without a terminal, forwards SIGTERM and SIGINT, exiting with the code', async (dir) => {
  for (const [signal, code] of [['SIGTERM', 42], ['SIGINT', 43]]) {
    const run = start(dir, 'bin/devdocs.js', [], { FAKE_ACTION: 'wait' }, { detached: true });
    await waitFor(run.child.stdout, 'ready');
    run.child.kill(signal);
    const result = await run.done;
    assert.equal(result.code, code, signal);
    assert.match(result.stdout, new RegExp(`^got ${signal.slice(3)}$`, 'm'));
  }
}, { timeout: 30000 });

launcherTest('under a terminal, Ctrl+C reaches the child once, all streams redirected', async (dir) => {
  const log = path.join(dir, 'log');
  const read = () => (fs.existsSync(log) ? fs.readFileSync(log, 'utf8') : '');
  const env = {
    PATH: process.env.PATH,
    DEVDOCS_PYTHON: path.join(dir, 'fake-python'),
    FAKE_ACTION: 'count',
    NODE: process.execPath,
    WRAPPER: path.join(dir, 'bin', 'devdocs.js'),
    LOG: log,
  };
  const line = 'exec "$NODE" "$WRAPPER" </dev/null >"$LOG" 2>&1';
  const terminal = spawn('/bin/sh', ['-c', 'cat | script "$@"', 'sh', ...onPty(line)], {
    env,
    stdio: ['pipe', 'ignore', 'ignore'], // cat: script refuses a socket as stdin
  });
  const closed = new Promise((resolve, reject) => {
    terminal.on('error', reject);
    terminal.on('close', resolve);
  });
  try {
    await until(() => read().includes('ready'));
    terminal.stdin.write('\x03');
    await until(() => read().includes('INT x'));
  } finally {
    terminal.stdin.end();
  }
  await closed;
  assert.match(read(), /^INT x1$/m, read());
}, { skip: PTY_ONLY, timeout: 30000 });

launcherTest('asks for npm run prepack when the bundle is missing', async (dir) => {
  fs.rmSync(path.join(dir, 'python'), { recursive: true });
  const result = await start(dir, 'bin/devdocs.js', ['--version'], {}).done;
  assert.equal(result.code, 1);
  assert.match(result.stderr, /bundled Python package is missing/);
  assert.match(result.stderr, /`npm run prepack`/);
});

launcherTest('says how to get Python when DEVDOCS_PYTHON does not run', async (dir) => {
  const missing = path.join(dir, 'missing-python');
  const result = await start(dir, 'bin/devdocs.js', ['--version'], { DEVDOCS_PYTHON: missing }).done;
  assert.equal(result.code, 1);
  assert.ok(result.stderr.includes(`DEVDOCS_PYTHON (${missing}) is older or does not run`));
  assert.match(result.stderr, /uv python install 3\.13/);
  assert.equal(result.stdout, '');
});

launcherTest('postinstall fails without Python unless DEVDOCS_SKIP_PYTHON_CHECK=1', async (dir) => {
  const missing = { DEVDOCS_PYTHON: path.join(dir, 'missing-python') };
  const failed = await start(dir, 'scripts/postinstall.js', [], missing).done;
  assert.equal(failed.code, 1);
  assert.match(failed.stderr, /Python 3\.11 or newer is required/);

  const skip = { ...missing, DEVDOCS_SKIP_PYTHON_CHECK: '1' };
  const skipped = await start(dir, 'scripts/postinstall.js', [], skip).done;
  assert.deepEqual([skipped.code, skipped.stderr], [0, '']);

  const found = await start(dir, 'scripts/postinstall.js', [], {}).done;
  assert.deepEqual([found.code, found.stderr], [0, '']);
});
