'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { bundle, npmVersion, readVersion } = require('../scripts/bundle');

/** A repository holding a devdocs package with caches, a LICENSE, and npm/ with a stale bundle. */
function repository(pythonVersion, packageVersion) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'devdocs-bundle-'));
  const files = {
    'devdocs/__init__.py': `"""devdocs."""\n\n__version__ = "${pythonVersion}"\n`,
    'devdocs/__main__.py': 'raise SystemExit(0)\n',
    'devdocs/py.typed': '',
    'devdocs/prompts/system.md': '# Prompt\n',
    'devdocs/llm/client.py': 'VALUE = 1\n',
    'devdocs/__pycache__/__init__.cpython-313.pyc': 'cache',
    'devdocs/llm/__pycache__/client.cpython-313.pyc': 'cache',
    'devdocs/llm/stray.pyc': 'cache',
    'devdocs/.DS_Store': 'finder',
    'devdocs/prompts/.DS_Store': 'finder',
    LICENSE: 'MIT License\n',
    'npm/package.json': JSON.stringify({ name: '@bgantavya/devdocs', version: packageVersion }),
    'npm/python/devdocs/removed.py': 'STALE = True\n',
  };
  for (const [name, content] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(root, name)), { recursive: true });
    fs.writeFileSync(path.join(root, name), content);
  }
  return root;
}

/** Every file under `dir`, as sorted relative paths with forward slashes. */
function listFiles(dir, prefix = '') {
  return fs.readdirSync(dir, { withFileTypes: true })
    .flatMap((entry) => (entry.isDirectory()
      ? listFiles(path.join(dir, entry.name), `${prefix}${entry.name}/`)
      : [`${prefix}${entry.name}`]))
    .sort();
}

test('maps every PEP 440 form to npm semver', () => {
  const rows = [
    ['4.0.0', '4.0.0'],
    ['4.0.0.dev0', '4.0.0-dev.0'],
    ['4.0.0a1', '4.0.0-alpha.1'],
    ['4.0.0b2', '4.0.0-beta.2'],
    ['4.0.0rc3', '4.0.0-rc.3'],
    ['10.20.30.dev12', '10.20.30-dev.12'],
  ];
  for (const [pep440, npm] of rows) assert.equal(npmVersion(pep440), npm, pep440);
});

test('refuses versions outside the mapping', () => {
  const invalid = [
    '', '4.0', '4.0.0.0', 'v4.0.0', ' 4.0.0', '04.0.0', '4.0.0-dev.0', '4.0.0dev0', '4.0.0.dev',
    '4.0.0.post1', '4.0.0+local', '4.0.0rc1.dev2', '4.0.0c1', '4.0.0RC1', undefined, null, 4,
    ['4.0.0'],
  ];
  for (const version of invalid) {
    assert.throws(() => npmVersion(version), /not a version npm can carry/, String(version));
  }
});

test('reads __version__ and refuses a file without one', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'devdocs-version-'));
  try {
    const file = path.join(dir, '__init__.py');
    fs.writeFileSync(file, "'''Docs.'''\n__version__ = '1.2.3b4'  # bumped by release.py\n");
    assert.equal(readVersion(file), '1.2.3b4');
    fs.writeFileSync(file, 'VERSION = "1.2.3"\n');
    assert.throws(() => readVersion(file), /no __version__/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('package.json carries the npm form of devdocs.__version__', () => {
  const init = path.join(__dirname, '..', '..', 'devdocs', '__init__.py');
  const manifest = path.join(__dirname, '..', 'package.json');
  const { version } = JSON.parse(fs.readFileSync(manifest, 'utf8'));
  assert.equal(version, npmVersion(readVersion(init)));
});

test('bundles the package without caches, replaces a stale bundle, and copies LICENSE', () => {
  const root = repository('1.2.3rc4', '1.2.3-rc.4');
  try {
    const npmDir = path.join(root, 'npm');
    assert.equal(bundle({ root, npmDir }), '1.2.3rc4');
    assert.deepEqual(listFiles(path.join(npmDir, 'python')), [
      'devdocs/__init__.py',
      'devdocs/__main__.py',
      'devdocs/llm/client.py',
      'devdocs/prompts/system.md',
      'devdocs/py.typed',
    ]);
    assert.equal(fs.readFileSync(path.join(npmDir, 'LICENSE'), 'utf8'), 'MIT License\n');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('refuses to bundle, copying nothing, when the versions disagree', () => {
  const root = repository('4.0.0.dev1', '4.0.0-dev.0');
  try {
    const npmDir = path.join(root, 'npm');
    assert.throws(() => bundle({ root, npmDir }), (error) => {
      assert.match(error.message, /npm\/package\.json has version 4\.0\.0-dev\.0/);
      assert.match(error.message, /__init__\.py has 4\.0\.0\.dev1, which is 4\.0\.0-dev\.1 for npm/);
      assert.match(error.message, /release\.py bump 4\.0\.0\.dev1/);
      return true;
    });
    assert.deepEqual(listFiles(path.join(npmDir, 'python')), ['devdocs/removed.py']);
    assert.equal(fs.existsSync(path.join(npmDir, 'LICENSE')), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
