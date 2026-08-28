'use strict';

/**
 * prepack: replaces python/devdocs with ../devdocs, caches left out, and copies ../LICENSE; only
 * when package.json carries the npm form of __version__, so the two never ship out of step.
 */

const fs = require('node:fs');
const path = require('node:path');

const PEP440 = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(\.dev|a|b|rc)(0|[1-9]\d*))?$/;
const PRERELEASE = { '.dev': 'dev', a: 'alpha', b: 'beta', rc: 'rc' };
const SKIP = new Set(['__pycache__', '.DS_Store']);

/** npm's form of a PEP 440 version: 4.0.0.dev0 is 4.0.0-dev.0, 4.0.0rc1 is 4.0.0-rc.1. */
function npmVersion(version) {
  const match = typeof version === 'string' && PEP440.exec(version);
  if (!match) throw new Error(`not a version npm can carry: ${JSON.stringify(version)}`);
  const [, major, minor, patch, kind, number] = match;
  const release = `${major}.${minor}.${patch}`;
  return kind ? `${release}-${PRERELEASE[kind]}.${number}` : release;
}

/** The __version__ assigned in a Python file. */
function readVersion(file) {
  const match = /^__version__\s*=\s*(["'])([^"']+)\1/m.exec(fs.readFileSync(file, 'utf8'));
  if (!match) throw new Error(`no __version__ in ${file}`);
  return match[2];
}

/** Bundles `root`'s devdocs package and LICENSE into `npmDir`; returns the version. */
function bundle({
  root = path.resolve(__dirname, '..', '..'),
  npmDir = path.resolve(__dirname, '..'),
} = {}) {
  const version = readVersion(path.join(root, 'devdocs', '__init__.py'));
  const expected = npmVersion(version);
  const manifest = JSON.parse(fs.readFileSync(path.join(npmDir, 'package.json'), 'utf8'));
  if (manifest.version !== expected) {
    throw new Error(
      `npm/package.json has version ${manifest.version}, but devdocs/__init__.py has `
      + `${version}, which is ${expected} for npm. `
      + `Run \`python scripts/release.py bump ${version}\` to align them.`,
    );
  }
  const target = path.join(npmDir, 'python');
  fs.rmSync(target, { recursive: true, force: true });
  fs.cpSync(path.join(root, 'devdocs'), path.join(target, 'devdocs'), {
    recursive: true,
    filter: (source) => !SKIP.has(path.basename(source)) && !source.endsWith('.pyc'),
  });
  fs.copyFileSync(path.join(root, 'LICENSE'), path.join(npmDir, 'LICENSE'));
  return version;
}

if (require.main === module) {
  try {
    console.log(`Bundled devdocs ${bundle()} into python/devdocs.`);
  } catch (error) {
    console.error(`bundle: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = { bundle, npmVersion, readVersion };
