'use strict';

/**
 * Finds the Python 3.11+ that runs the bundled devdocs: DEVDOCS_PYTHON alone when set, else the
 * newest versioned command first, after the py launcher on Windows.
 */

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const MINIMUM = [3, 11];
const NEWEST_MINOR = 15;
const PROBE_TIMEOUT_MS = 5000;
const PROBE = 'import sys; print("%d.%d" % sys.version_info[:2])';
const BARE = /^[^\\/:]+$/; // Windows looks such a name up in the working directory first

/** The interpreters to try, in order, as {command, args}. */
function candidates(env, platform) {
  if (env.DEVDOCS_PYTHON) return [{ command: env.DEVDOCS_PYTHON, args: [] }];
  const minors = Array.from(
    { length: NEWEST_MINOR - MINIMUM[1] + 1 },
    (_, index) => NEWEST_MINOR - index,
  );
  const versioned = minors.map((minor) => ({ command: `python3.${minor}`, args: [] }));
  if (platform === 'win32') {
    return [
      ...minors.map((minor) => ({ command: 'py', args: [`-3.${minor}`] })),
      { command: 'py', args: ['-3'] },
      ...versioned,
      { command: 'python', args: [] },
      { command: 'python3', args: [] },
    ];
  }
  return [...versioned, { command: 'python3', args: [] }, { command: 'python', args: [] }];
}

/** Whether `dir` names one place whatever the working directory: /x, C:\x or \\server\x. */
function isQualified(dir, platform = process.platform) {
  return platform === 'win32' ? /^(?:[A-Za-z]:|[\\/])[\\/]/.test(dir) : dir.startsWith('/');
}

/** Whether something other than a folder is at `file`; lstat also sees app execution aliases. */
function fileExists(file) {
  try {
    const stats = fs.lstatSync(file, { throwIfNoEntry: false });
    return Boolean(stats) && !stats.isDirectory();
  } catch {
    return false;
  }
}

/**
 * The file a bare Windows `name` runs, tried as libuv tries it (name if it has an extension,
 * then .com, then .exe) but only in fully qualified PATH folders, so a program planted in the
 * working directory never runs; null when there is none.
 */
function locate(name, env, exists) {
  const [, value = ''] = Object.entries(env).find(([key]) => key.toUpperCase() === 'PATH') ?? [];
  const files = [...(path.win32.extname(name) ? [name] : []), `${name}.com`, `${name}.exe`];
  for (const entry of value.split(';')) {
    const dir = entry.replace(/^["']|["']$/g, '');
    if (!isQualified(dir, 'win32')) continue;
    for (const file of files) {
      const full = path.win32.join(dir, file);
      if (exists(full)) return full;
    }
  }
  return null;
}

/** Runs one probe: no shell, no console window, killed outright on timeout. */
function spawnProbe(command, args, timeout) {
  return spawnSync(command, args, {
    encoding: 'utf8',
    killSignal: 'SIGKILL',
    stdio: ['ignore', 'pipe', 'ignore'],
    timeout,
    windowsHide: true,
  });
}

/** The [major, minor] a candidate reports, or null when it does not answer. */
function probe({ command, args }, run, timeout) {
  let result;
  try {
    result = run(command, [...args, '-c', PROBE], timeout);
  } catch {
    return null;
  }
  if (!result || result.error || result.status !== 0) return null;
  const match = /^(\d+)\.(\d+)$/.exec(String(result.stdout ?? '').trim());
  return match ? [Number(match[1]), Number(match[2])] : null;
}

/**
 * The first candidate running Python 3.11 or newer, as {command, args, version} with a version
 * such as "3.13", or null. On Windows the command is the absolute path `locate` found.
 * `run(command, args, timeoutMs)` and `exists(file)` stand in for the probe and lstat in tests.
 */
function findPython({
  env = process.env,
  platform = process.platform,
  run = spawnProbe,
  exists = fileExists,
  timeout = PROBE_TIMEOUT_MS,
} = {}) {
  for (const candidate of candidates(env, platform)) {
    const command = platform === 'win32' && BARE.test(candidate.command)
      ? locate(candidate.command, env, exists)
      : candidate.command;
    if (!command) continue;
    const version = probe({ ...candidate, command }, run, timeout);
    if (!version) continue;
    const [major, minor] = version;
    if (major > MINIMUM[0] || (major === MINIMUM[0] && minor >= MINIMUM[1])) {
      return { command, args: candidate.args, version: `${major}.${minor}` };
    }
  }
  return null;
}

/** What to tell the user when findPython finds nothing. */
function notFoundMessage(env = process.env) {
  const minimum = MINIMUM.join('.');
  const install = 'Install it from https://www.python.org/downloads/'
    + ' or with `uv python install 3.13`,';
  if (env.DEVDOCS_PYTHON) {
    return [
      `devdocs: Python ${minimum} or newer is required, `
        + `and DEVDOCS_PYTHON (${env.DEVDOCS_PYTHON}) is older or does not run.`,
      install,
      'then set DEVDOCS_PYTHON to its path, or unset DEVDOCS_PYTHON to search PATH.',
    ].join('\n');
  }
  return [
    `devdocs: Python ${minimum} or newer is required, and none was found on PATH.`,
    install,
    `or set DEVDOCS_PYTHON to the path of a Python ${minimum}+ interpreter.`,
  ].join('\n');
}

module.exports = { fileExists, findPython, isQualified, notFoundMessage };
