#!/usr/bin/env node
// L.E.O CLI is a Python program. This launcher lets npm and npx install it: it finds uv (Astral's Python package
// manager), installing it if needed, installs L.E.O CLI with `uv tool install` on first use, then runs it with the
// same arguments. `leo update` upgrades it in place.
'use strict';
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const SOURCE = process.env.LEO_CLI_SOURCE || 'git+https://github.com/albertleooonardi/L.E.O#subdirectory=leo-cli';
const windows = process.platform === 'win32';
const exe = name => (windows ? `${name}.exe` : name);

function say(message) {
  process.stderr.write(`${message}\n`);
}

function run(command, args) {
  const result = spawnSync(command, args, { stdio: 'inherit' });
  if (result.error) throw result.error;
  return result.status ?? 1;
}

function findUv() {
  const onPath = spawnSync(exe('uv'), ['--version'], { stdio: 'ignore' });
  if (!onPath.error && onPath.status === 0) return exe('uv');
  for (const folder of [path.join(os.homedir(), '.local', 'bin'), path.join(os.homedir(), '.cargo', 'bin')]) {
    const candidate = path.join(folder, exe('uv'));
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

function installUv() {
  say('L.E.O CLI runs on uv (https://docs.astral.sh/uv/), which is not installed. Installing it for your user…');
  const status = windows
    ? run('powershell', ['-NoProfile', '-ExecutionPolicy', 'ByPass', '-Command', 'irm https://astral.sh/uv/install.ps1 | iex'])
    : run('sh', ['-c', 'curl -LsSf https://astral.sh/uv/install.sh | sh']);
  const uv = status === 0 ? findUv() : null;
  if (!uv) {
    say('Could not install uv. Install it yourself (https://docs.astral.sh/uv/getting-started/installation/), then run leo again.');
    process.exit(1);
  }
  return uv;
}

function leoPath(uv) {
  const bin = spawnSync(uv, ['tool', 'dir', '--bin'], { encoding: 'utf8' });
  return bin.status === 0 ? path.join(bin.stdout.trim(), exe('leo')) : null;
}

function main() {
  const uv = findUv() || installUv();
  let leo = leoPath(uv);
  if (!leo || !fs.existsSync(leo)) {
    say('Installing L.E.O CLI (first run only)…');
    if (run(uv, ['tool', 'install', '--force', SOURCE]) !== 0) process.exit(1);
    leo = leoPath(uv);
  }
  // Ctrl-C reaches leo directly (same console); the launcher just waits for it and passes its exit code on.
  process.on('SIGINT', () => {});
  const child = spawn(leo, process.argv.slice(2), { stdio: 'inherit' });
  child.on('error', error => {
    say(`Could not start leo: ${error.message}`);
    process.exit(1);
  });
  child.on('exit', (code, signal) => process.exit(code ?? (signal ? 130 : 1)));
}

main();
