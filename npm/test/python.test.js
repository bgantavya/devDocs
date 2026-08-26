'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { fileExists, findPython, isQualified, notFoundMessage } = require('../lib/python');

const POSIX_ONLY = process.platform === 'win32' && 'uses POSIX shell scripts';

function errno(code) {
  return Object.assign(new Error(`spawnSync ${code}`), { code });
}

/**
 * A probe runner answering from `answers`, keyed by command line without the
 * probe code: a version string, or a function returning a spawnSync result.
 * Commands it has no answer for do not exist.
 */
function fakeRunner(answers = {}) {
  const calls = [];
  const run = (command, args, timeout) => {
    const line = [command, ...args.slice(0, -2)].join(' ');
    calls.push({ line, args, timeout });
    const answer = answers[line];
    if (answer === undefined) return { status: null, error: errno('ENOENT') };
    if (typeof answer === 'function') return answer();
    return { status: 0, stdout: `${answer}\n` };
  };
  return { run, calls, lines: () => calls.map((call) => call.line) };
}

/** A Windows PATH of `dirs` holding `files`; `checked` lists every path the lookup tried. */
function windowsPath(dirs, files) {
  const checked = [];
  const exists = (file) => {
    checked.push(file);
    return files.includes(file);
  };
  return { env: { Path: dirs.join(';') }, exists, checked };
}

test('POSIX order: python3.15 down to python3.11, then python3 and python', () => {
  for (const platform of ['linux', 'darwin']) {
    const runner = fakeRunner();
    assert.equal(findPython({ env: {}, platform, run: runner.run }), null);
    assert.deepEqual(runner.lines(), [
      'python3.15', 'python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3', 'python',
    ]);
  }
});

test('Windows order: py launcher, python3.15 down to python3.11, python, python3', () => {
  const names = ['py', 'python3.15', 'python3.14', 'python3.13', 'python3.12', 'python3.11'];
  const files = [...names, 'python', 'python3'].map((name) => `C:\\Tools\\${name}.exe`);
  const { env, exists } = windowsPath(['C:\\Tools'], files);
  const runner = fakeRunner();
  assert.equal(findPython({ env, platform: 'win32', run: runner.run, exists }), null);
  assert.deepEqual(runner.lines(), [
    'py.exe -3.15', 'py.exe -3.14', 'py.exe -3.13', 'py.exe -3.12', 'py.exe -3.11', 'py.exe -3',
    'python3.15.exe', 'python3.14.exe', 'python3.13.exe', 'python3.12.exe', 'python3.11.exe',
    'python.exe', 'python3.exe',
  ].map((line) => `C:\\Tools\\${line}`));
});

test('Windows looks a bare name up in fully qualified PATH folders only', () => {
  const dirs = ['', '.', 'tools', 'C:relative', '\\rooted', '"C:\\Program Files\\Py"', 'D:/bin'];
  const planted = ['py.exe', 'tools\\py.exe', 'C:relative\\py.exe', '\\rooted\\py.exe'];
  const { env, exists, checked } = windowsPath(dirs, [...planted, 'D:\\bin\\py.exe']);
  const runner = fakeRunner({ 'D:\\bin\\py.exe -3.15': '3.15' });
  assert.deepEqual(
    findPython({ env, platform: 'win32', run: runner.run, exists }),
    { command: 'D:\\bin\\py.exe', args: ['-3.15'], version: '3.15' },
  );
  assert.deepEqual(checked, [
    'C:\\Program Files\\Py\\py.com', 'C:\\Program Files\\Py\\py.exe',
    'D:\\bin\\py.com', 'D:\\bin\\py.exe',
  ]);
});

test('Windows looks up a bare DEVDOCS_PYTHON the same way and runs a path as given', () => {
  const files = ['python3.13.exe', 'C:\\Py\\python3.13.exe', 'C:\\Py\\python.exe'];
  const { env, exists } = windowsPath(['.', 'C:\\Py'], files);
  for (const [name, command] of [
    ['python3.13', 'C:\\Py\\python3.13.exe'],
    ['python.exe', 'C:\\Py\\python.exe'],
    ['.\\venv\\Scripts\\python.exe', '.\\venv\\Scripts\\python.exe'],
  ]) {
    const runner = fakeRunner({ [command]: '3.13' });
    const options = { env: { ...env, DEVDOCS_PYTHON: name }, platform: 'win32', run: runner.run };
    const found = findPython({ ...options, exists });
    assert.deepEqual(found, { command, args: [], version: '3.13' }, name);
  }
});

test('Windows never runs a name it cannot find on PATH', () => {
  const runner = fakeRunner();
  const { env, exists } = windowsPath(['C:\\Empty'], []);
  assert.equal(findPython({ env, platform: 'win32', run: runner.run, exists }), null);
  const anything = () => true;
  assert.equal(findPython({ env: {}, platform: 'win32', run: runner.run, exists: anything }), null);
  assert.deepEqual(runner.lines(), []);
});

test('asks each candidate for sys.version_info[:2]', () => {
  const runner = fakeRunner({ 'C:\\Windows\\py.exe -3.13': '3.13' });
  const exists = (file) => file === 'C:\\Windows\\py.exe';
  findPython({ env: { PATH: 'C:\\Windows' }, platform: 'win32', run: runner.run, exists });
  const { args } = runner.calls.at(-1);
  assert.deepEqual(args.slice(0, 2), ['-3.13', '-c']);
  assert.match(args[2], /sys\.version_info\[:2\]/);
});

test('takes the first candidate that is new enough and stops there', () => {
  const runner = fakeRunner({ 'python3.12': '3.12', python3: '3.14' });
  assert.deepEqual(
    findPython({ env: {}, platform: 'linux', run: runner.run }),
    { command: 'python3.12', args: [], version: '3.12' },
  );
  assert.deepEqual(runner.lines(), ['python3.15', 'python3.14', 'python3.13', 'python3.12']);

  const windows = fakeRunner({ 'C:\\Windows\\py.exe -3': '3.14' });
  const { env, exists } = windowsPath(['C:\\Windows'], ['C:\\Windows\\py.exe']);
  assert.deepEqual(
    findPython({ env, platform: 'win32', run: windows.run, exists }),
    { command: 'C:\\Windows\\py.exe', args: ['-3'], version: '3.14' },
  );
});

test('rejects Python 3.10 and older', () => {
  const old = fakeRunner({ python3: '3.10', python: '2.7' });
  assert.equal(findPython({ env: {}, platform: 'linux', run: old.run }), null);

  const boundary = fakeRunner({ python3: '3.10', python: '3.11' });
  assert.deepEqual(
    findPython({ env: {}, platform: 'linux', run: boundary.run }),
    { command: 'python', args: [], version: '3.11' },
  );
});

test('DEVDOCS_PYTHON is the only candidate when set', () => {
  const env = { DEVDOCS_PYTHON: 'C:\\Program Files\\Python313\\python.exe' };
  const runner = fakeRunner({ [env.DEVDOCS_PYTHON]: '3.13', 'py -3.13': '3.13' });
  assert.deepEqual(
    findPython({ env, platform: 'win32', run: runner.run }),
    { command: env.DEVDOCS_PYTHON, args: [], version: '3.13' },
  );
  assert.deepEqual(runner.lines(), [env.DEVDOCS_PYTHON]);
});

test('a DEVDOCS_PYTHON that is too old or missing is an error, not a fallback', () => {
  const env = { DEVDOCS_PYTHON: '/usr/bin/python3' };
  for (const answer of ['3.9', undefined]) {
    const runner = fakeRunner({ [env.DEVDOCS_PYTHON]: answer, 'python3.13': '3.13', python3: '3.13' });
    assert.equal(findPython({ env, platform: 'darwin', run: runner.run }), null);
    assert.deepEqual(runner.lines(), [env.DEVDOCS_PYTHON]);
  }
});

test('an empty DEVDOCS_PYTHON is ignored', () => {
  const runner = fakeRunner({ python3: '3.13' });
  const found = findPython({ env: { DEVDOCS_PYTHON: '' }, platform: 'linux', run: runner.run });
  assert.equal(found.command, 'python3');
});

test('skips probes that time out, crash, fail, or answer nonsense', () => {
  const runner = fakeRunner({
    'python3.15': () => ({ status: null, signal: 'SIGKILL', error: errno('ETIMEDOUT') }),
    'python3.14': () => ({ status: null, signal: 'SIGSEGV', stdout: '' }),
    'python3.13': () => {
      throw new Error('runner failed');
    },
    'python3.12': () => ({ status: 9009, stdout: '' }),
    'python3.11': () => ({ status: 0, stdout: 'Python was not found; run without arguments.' }),
    python3: '3.11',
  });
  assert.deepEqual(
    findPython({ env: {}, platform: 'linux', run: runner.run, timeout: 1234 }),
    { command: 'python3', args: [], version: '3.11' },
  );
  assert.ok(runner.calls.every((call) => call.timeout === 1234));
});

test('real probes skip a hanging, crashing, old or missing interpreter', { skip: POSIX_ONLY }, () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'devdocs-probe-'));
  const scripts = {
    hang: 'exec sleep 30',
    crash: 'kill -KILL $$',
    old: 'echo 3.10',
    good: 'echo 3.13',
  };
  for (const [name, body] of Object.entries(scripts)) {
    fs.writeFileSync(path.join(dir, name), `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  }
  const find = (name, timeout) => {
    const env = { DEVDOCS_PYTHON: path.join(dir, name) };
    return findPython({ env, timeout });
  };
  try {
    const started = Date.now();
    assert.equal(find('hang', 200), null);
    assert.ok(Date.now() - started < 5000, 'the hanging probe was not cut short');
    assert.equal(find('crash'), null);
    assert.equal(find('old'), null);
    assert.equal(find('missing'), null);
    assert.deepEqual(find('good'), { command: path.join(dir, 'good'), args: [], version: '3.13' });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('a qualified folder is the same place whatever the working directory', () => {
  for (const dir of ['/usr/lib', '/']) assert.ok(isQualified(dir, 'linux'), dir);
  for (const dir of ['', '.', 'lib', './lib', '~/lib']) assert.ok(!isQualified(dir, 'darwin'), dir);
  for (const dir of ['C:\\x', 'c:/x', '\\\\server\\share', '//server/share']) {
    assert.ok(isQualified(dir, 'win32'), dir);
  }
  for (const dir of ['', '.', 'x', 'C:', 'C:x', '\\x', '/x', '"C:\\x"']) {
    assert.ok(!isQualified(dir, 'win32'), dir);
  }
});

test('fileExists sees files and links, not folders or missing paths', { skip: POSIX_ONLY }, () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'devdocs-exists-'));
  try {
    fs.writeFileSync(path.join(dir, 'file'), '');
    fs.symlinkSync(path.join(dir, 'nowhere'), path.join(dir, 'link')); // as lstat sees an alias
    assert.equal(fileExists(path.join(dir, 'file')), true);
    assert.equal(fileExists(path.join(dir, 'link')), true);
    assert.equal(fileExists(dir), false);
    assert.equal(fileExists(path.join(dir, 'missing')), false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('the message says what is needed, where to get it, and DEVDOCS_PYTHON', () => {
  const message = notFoundMessage({});
  for (const part of [
    'Python 3.11 or newer is required',
    'https://www.python.org/downloads/',
    '`uv python install 3.13`',
    'set DEVDOCS_PYTHON',
  ]) {
    assert.ok(message.includes(part), `missing: ${part}`);
  }
  const explicit = notFoundMessage({ DEVDOCS_PYTHON: '/usr/bin/python3' });
  assert.match(explicit, /DEVDOCS_PYTHON \(\/usr\/bin\/python3\) is older or does not run/);
  assert.match(explicit, /unset DEVDOCS_PYTHON/);
});
