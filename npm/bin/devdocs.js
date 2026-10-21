#!/usr/bin/env node
'use strict';

/**
 * `devdocs` from npm: `python -P -m devdocs` on the first Python 3.11+, the bundle leading
 * PYTHONPATH, exiting as the child does; SIGTERM is passed on, SIGINT as forwardsSigint decides.
 */

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { findPython, isQualified, notFoundMessage } = require('../lib/python');

const BUNDLE = path.join(__dirname, '..', 'python');

/** Reports a failure; the process then ends by itself, with stderr flushed. */
function fail(message) {
  console.error(message);
  process.exitCode = 1;
}

/** Whether this process has a controlling terminal, whose Ctrl+C reaches the child too. */
function hasControllingTerminal() {
  try {
    fs.closeSync(fs.openSync('/dev/tty', 'r'));
    return true;
  } catch {
    return false;
  }
}

/**
 * Whether to pass a SIGINT on: never on Windows, where kill() ends the child outright, nor under
 * a controlling terminal, whose Ctrl+C already reached the child. A copy sent within 1 s would be
 * harmless, one request to devdocs, but a stalled wrapper's later copy would read as a second
 * request and stop it at once. A SIGINT sent to this process alone then stays here.
 */
function forwardsSigint(platform = process.platform, terminal = hasControllingTerminal) {
  return platform !== 'win32' && !terminal();
}

function main() {
  if (!fs.existsSync(path.join(BUNDLE, 'devdocs', '__main__.py'))) {
    fail(
      `devdocs: the bundled Python package is missing from ${BUNDLE}.\n`
      + 'In a checkout, run `npm run prepack` in npm/ first.',
    );
    return;
  }
  if (BUNDLE.includes(path.delimiter)) {
    fail(
      `devdocs: cannot run from ${BUNDLE}: PYTHONPATH cannot hold a path containing`
      + ` "${path.delimiter}". Install @bgantavya/devdocs in a folder without it.`,
    );
    return;
  }
  const python = findPython();
  if (!python) {
    fail(notFoundMessage());
    return;
  }

  const env = { ...process.env };
  const inherited = (process.env.PYTHONPATH ?? '') // process.env matches any case on Windows
    .split(path.delimiter)
    .filter((dir) => isQualified(dir)); // empty or relative: resolved in the working directory
  env.PYTHONPATH = [BUNDLE, ...inherited].join(path.delimiter);
  env.PYTHONDONTWRITEBYTECODE ??= '1'; // the package folder may be read-only
  const child = spawn(
    python.command,
    [...python.args, '-P', '-m', 'devdocs', ...process.argv.slice(2)],
    { env, stdio: 'inherit' },
  );

  process.on('SIGINT', () => {
    if (forwardsSigint()) child.kill('SIGINT');
  });
  process.on('SIGTERM', () => child.kill('SIGTERM'));
  child.on('error', (error) => fail(`devdocs: could not run ${python.command}: ${error.message}`));
  child.on('exit', (code, signal) => {
    process.exit(signal ? 128 + os.constants.signals[signal] : code);
  });
}

if (require.main === module) main();

module.exports = { forwardsSigint };
