'use strict';

/**
 * The checkout's CI files against each other, so they cannot drift apart unseen. They live with
 * the npm tests, which CI runs, and the package ships none of them.
 */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const ROOT = path.join(__dirname, '..', '..');

/** A file of the checkout, with LF line ends whatever core.autocrlf made them. */
function read(file) {
  return fs.readFileSync(path.join(ROOT, file), 'utf8').replace(/\r\n/g, '\n');
}

test('pip-audit is given every extra, in CI and in make security', () => {
  for (const file of ['Makefile', '.github/workflows/ci.yml']) {
    const commands = read(file).split('\n')
      .filter((line) => /(?:\buv|\$\(UV\)) export\b/.test(line));
    assert.ok(commands.length > 0, `${file} exports no requirements`);
    for (const line of commands) assert.match(line, / --all-extras\b/, `${file}: ${line}`);
  }
});
