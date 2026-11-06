'use strict';

/**
 * The checkout's container and CI files against the code they configure, so they cannot drift
 * apart unseen; nothing here runs Docker. They live with the npm tests, the ones CI runs with
 * Python and Node both at hand, and the package ships none of them.
 */

const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { findPython } = require('../lib/python');

const ROOT = path.join(__dirname, '..', '..');
const PYTHON = findPython();
const NEEDS_PYTHON = !PYTHON && 'needs Python 3.11 or newer';

/** Prints the variables config.py looks up, recorded from an environment with none set. */
const CONFIG_LOOKUPS = `
import json, os, pathlib
from collections.abc import Mapping
from devdocs.config import Config, load_config, resolve_api_key
from devdocs.llm.factory import PROVIDERS

asked = set()

class Recording(Mapping):
    def __getitem__(self, key):
        asked.add(key)
        raise KeyError(key)
    def __iter__(self):
        return iter(())
    def __len__(self):
        return 0

load_config(config_file=pathlib.Path(os.devnull), env=Recording())
for provider in PROVIDERS:
    resolve_api_key(Config(provider=provider), env=Recording(), demo_key="demo")
print(json.dumps(sorted(asked)))
`;

/** A file of the checkout, with LF line ends whatever core.autocrlf made them. */
function read(file) {
  return fs.readFileSync(path.join(ROOT, file), 'utf8').replace(/\r\n/g, '\n');
}

/** `text` with its comment lines run together, so a phrase matches however they wrap. */
function prose(text) {
  return text.replace(/\n#\s*/g, ' ');
}

/**
 * The environment variables devdocs reads: config.py's, recorded as it looks them up, and every
 * DEVDOCS_* name quoted anywhere in the package, however it is read. Of the names built at run
 * time, only config.py's are found.
 */
function lookups() {
  const result = spawnSync(PYTHON.command, [...PYTHON.args, '-c', CONFIG_LOOKUPS], {
    cwd: ROOT,
    encoding: 'utf8',
    env: { ...process.env, PYTHONPATH: ROOT, PYTHONDONTWRITEBYTECODE: '1' },
  });
  assert.equal(result.status, 0, result.stderr);
  const quoted = fs.readdirSync(path.join(ROOT, 'devdocs'), { recursive: true })
    .filter((file) => file.endsWith('.py'))
    .flatMap((file) => Array.from(
      read(path.join('devdocs', file)).matchAll(/["'](DEVDOCS_[A-Z0-9_]+)["']/g),
      (match) => match[1],
    ));
  return [...new Set([...JSON.parse(result.stdout), ...quoted])];
}

/** The entries of compose.yaml's `key` list, such as environment; none if it has no such list. */
function composeList(key) {
  const list = new RegExp(`^( +)${key}:\\n((?:\\1 +.*\\n)+)`, 'm').exec(read('compose.yaml'));
  return (list ? list[2].split('\n') : [])
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'))
    .map((line) => line.replace(/^- /, ''));
}

test('compose.yaml passes on, by bare name, every key and setting devdocs reads', { skip: NEEDS_PYTHON }, () => {
  const entries = composeList('environment');
  assert.ok(entries.length > 0, 'compose.yaml has no environment list');
  for (const entry of entries) {
    assert.match(entry, /^[A-Z][A-Z0-9_]*$/, `${entry}: with a value, unset would not stay unset`);
  }
  const names = lookups();
  assert.deepEqual(names.filter((name) => !entries.includes(name)), []); // silently dropped
  const settings = entries.filter((name) => name.startsWith('DEVDOCS_'));
  assert.deepEqual(settings.filter((name) => !names.includes(name)), []); // read by nothing
});

test('the path note names each tmpfs, where pages are written and then lost', () => {
  const compose = read('compose.yaml');
  assert.match(compose, /^ +read_only: true$/m, 'the note says most paths elsewhere are refused');
  const named = prose(compose).matchAll(/(\S+) is the container's tmpfs\b/g);
  const mounts = composeList('tmpfs').map((entry) => entry.split(':')[0]); // /tmp:size=64m is /tmp
  assert.deepEqual(Array.from(named, (match) => match[1]), mounts);
});

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
