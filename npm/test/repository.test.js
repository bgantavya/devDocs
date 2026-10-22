'use strict';

/**
 * The checkout's container and CI files against the code they configure, so they cannot drift
 * apart unseen; nothing here runs Docker. They live with the npm tests, which CI runs, and the
 * package ships none of them.
 */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const ROOT = path.join(__dirname, '..', '..');

/** A file of the checkout, with LF line ends whatever core.autocrlf made them. */
function read(file) {
  return fs.readFileSync(path.join(ROOT, file), 'utf8').replace(/\r\n/g, '\n');
}

/** `text` with its comment lines run together, so a phrase matches however they wrap. */
function prose(text) {
  return text.replace(/\n#\s*/g, ' ');
}

test('the stop advice names one signal, its exit status and one grace period, past the HTTP timeout', () => {
  const dockerfile = read('Dockerfile');
  const compose = read('compose.yaml');
  const signal = /^STOPSIGNAL (\S+)$/m.exec(dockerfile)[1];
  assert.equal(/^ +stop_signal: (\S+)$/m.exec(compose)[1], signal);
  const status = 128 + os.constants.signals[signal]; // as devdocs exits
  for (const [text, sends] of [[dockerfile, '`docker stop` sends'], [compose, 'Stopping sends']]) {
    assert.match(prose(text), new RegExp(`${sends} ${signal}\\b`));
    assert.match(prose(text), new RegExp(`\\bexits ${status}\\b`));
  }
  const [grace, ...others] = [
    /stop_grace_period: (\d+)s$/m.exec(compose),
    /docker stop -t (\d+)/.exec(dockerfile),
    /docker run --stop-timeout (\d+)/.exec(dockerfile),
  ].map((match) => Number(match[1]));
  assert.deepEqual(others, [grace, grace]);
  const timeout = Number(/\btimeout: float = ([\d.]+)/.exec(read('devdocs/llm/http.py'))[1]);
  assert.ok(grace > timeout, `${grace} s leaves a call on the wire no time to finish`);
  assert.match(prose(compose), new RegExp(`\\(${timeout} s HTTP timeout\\)`));
});

test('pip-audit is given every extra, in CI and in make security', () => {
  for (const file of ['Makefile', '.github/workflows/ci.yml']) {
    const commands = read(file).split('\n')
      .filter((line) => /(?:\buv|\$\(UV\)) export\b/.test(line));
    assert.ok(commands.length > 0, `${file} exports no requirements`);
    for (const line of commands) assert.match(line, / --all-extras\b/, `${file}: ${line}`);
  }
});
