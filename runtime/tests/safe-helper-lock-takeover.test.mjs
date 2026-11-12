import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { fork, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';

const source = fs.readFileSync(process.env.BASELINE_SOURCE ??
  new URL('../../app/android/app/src/main/kotlin/io/github/ccch1mneyyy/dsh_app/runtime/SafeMode.kt', import.meta.url), 'utf8');
const script = source.match(/const val PLUGINS_SCRIPT = """([\s\S]*?)"""/)[1];
const worker = fileURLToPath(new URL('./fixtures/safe-helper-lock-worker.mjs', import.meta.url));
const { withFileLock, writeFileAtomic } = await import(pathToFileURL(join(resolve(process.env.DSH_PREFIX),
  'node_modules/@deepseek-ai/dsh-atomic-write/lib/index.js')));

function fixture(t) {
  const home = fs.mkdtempSync(join(tmpdir(), 'safe-helper-takeover-'));
  const file = join(home, 'profiles/web/package.json');
  const helper = join(home, 'helper.cjs');
  const children = [];
  fs.mkdirSync(dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify({
    dependencies: { 'helper-plugin': '1.0.0', 'other-plugin': '1.0.0' },
    dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', 'helper-plugin', 'other-plugin'] } },
  }));
  fs.writeFileSync(helper, script);
  t.after(async () => {
    for (const child of children) {
      if (child.process.exitCode === null && child.process.signalCode === null) child.process.kill('SIGKILL');
    }
    await Promise.all(children.map(child => child.closed));
    fs.rmSync(home, { recursive: true, force: true });
  });
  function start(entry, args) {
    const childProcess = fork(entry, args, {
      env: { ...process.env, DSH_HOME: home, DSH_APP_ORIGINAL_HOME: home },
      stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
    });
    const child = { process: childProcess, closed: once(childProcess, 'close'), stdout: '', stderr: '' };
    childProcess.stdout.on('data', bytes => { child.stdout += bytes; });
    childProcess.stderr.on('data', bytes => { child.stderr += bytes; });
    children.push(child);
    return child;
  }
  return { home, file, helper, start };
}

async function deadLock(f) {
  const holder = f.start(worker, ['holder', f.file]);
  const [message] = await Promise.race([
    once(holder.process, 'message', { signal: AbortSignal.timeout(4000) }),
    holder.closed.then(() => { throw new Error('fixture: holder exited before locking: ' + holder.stderr); }),
  ]);
  assert.equal(message.type, 'held');
  const record = fs.readFileSync(f.file + '.lock', 'utf8');
  assert.equal(record, holder.process.pid + '\n');
  holder.process.kill('SIGKILL');
  assert.deepEqual(await holder.closed, [null, 'SIGKILL']);
  assert.equal(holder.stderr, '');
  assert.throws(() => process.kill(holder.process.pid, 0), { code: 'ESRCH' });
  return record;
}

async function reportAt(file, child) {
  const deadline = Date.now() + 4000;
  for (;;) {
    try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (child.process.exitCode !== null || child.process.signalCode !== null) {
      throw new Error('fixture: helper exited before ' + file + ': ' + child.stderr);
    }
    if (Date.now() >= deadline) throw new Error('fixture: no report at ' + file);
    await delay(5);
  }
}

function lockNames(file) {
  return fs.readdirSync(dirname(file)).filter(name => name.startsWith('package.json.lock'));
}

test('the real safe helper can recover a lock left by a killed production writer', { timeout: 10000 }, async t => {
  const f = fixture(t);
  await deadLock(f);
  const child = f.start(f.helper, ['disable', 'helper-plugin']);
  assert.deepEqual(await child.closed, [0, null], child.stderr);
  assert.equal(child.stderr, '');
  assert.deepEqual(JSON.parse(fs.readFileSync(f.file, 'utf8')).dsh.profile.bundles,
    ['@deepseek-ai/dsh-base', 'other-plugin']);
  assert.deepEqual(lockNames(f.file), []);
});

test('a kernel claim-write failure preserves the dead lock and permits an unlimited retry',
  { skip: process.platform !== 'linux', timeout: 15000 }, async t => {
    const f = fixture(t), record = await deadLock(f);
    const before = fs.readFileSync(f.file, 'utf8');
    // Apply only this child's real kernel limit; exclusive creation succeeds,
    // but the claim PID write fails. No fs method or error object is replaced.
    const limit = [
      'import os, resource, signal, sys',
      'signal.signal(signal.SIGXFSZ, signal.SIG_IGN)',
      '_, hard = resource.getrlimit(resource.RLIMIT_FSIZE)',
      'resource.setrlimit(resource.RLIMIT_FSIZE, (0, hard))',
      'os.execv(sys.argv[1], sys.argv[1:])',
    ].join('\n');
    const failed = spawnSync('python3', ['-I', '-c', limit, process.execPath, f.helper, 'disable', 'helper-plugin'], {
      encoding: 'utf8', timeout: 10000,
      env: { ...process.env, DSH_HOME: f.home, DSH_APP_ORIGINAL_HOME: f.home },
    });
    assert.ifError(failed.error);
    assert.notEqual(failed.status, 0);
    assert.match(failed.stderr, /EFBIG.*write/);
    let remaining;
    try { remaining = fs.readFileSync(f.file + '.lock', 'utf8'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; remaining = null; }
    const afterFailure = { record: remaining, locks: lockNames(f.file), manifest: fs.readFileSync(f.file, 'utf8') };
    t.diagnostic(JSON.stringify({ kernel: 'EFBIG', recordPreserved: remaining === record, locks: afterFailure.locks }));
    const retried = f.start(f.helper, ['disable', 'helper-plugin']);
    assert.deepEqual(await retried.closed, [0, null], retried.stderr);
    assert.equal(retried.stderr, '');
    assert.deepEqual(afterFailure, { record, locks: ['package.json.lock'], manifest: before });
    assert.deepEqual(JSON.parse(fs.readFileSync(f.file, 'utf8')).dsh.profile.bundles,
      ['@deepseek-ai/dsh-base', 'other-plugin']);
    assert.deepEqual(lockNames(f.file), []);
  });

test('a late dead-record reader preserves the live successor lock and both manifest updates', { timeout: 10000 }, async t => {
  const f = fixture(t), record = await deadLock(f), gate = join(f.home, 'schedule');
  const child = f.start(worker, ['helper', f.file, f.helper, gate]);
  assert.deepEqual(await reportAt(gate + '.read', child), { record });
  let observed;
  await withFileLock(f.file, async () => {
    assert.equal(fs.readFileSync(f.file + '.lock', 'utf8'), process.pid + '\n');
    const manifest = JSON.parse(fs.readFileSync(f.file, 'utf8'));
    fs.writeFileSync(gate + '.go', '1');
    const attempt = await reportAt(gate + '.attempt', child);
    let liveRecord;
    try { liveRecord = fs.readFileSync(f.file + '.lock', 'utf8'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; liveRecord = null; }
    observed = { attempt, liveRecord };
    // If the broken helper entered, let its real update finish first so this
    // writer's already-read manifest demonstrates the lost update. Otherwise
    // release this live lock before awaiting the correctly blocked helper.
    // Both schedules use exactly the same assertions below.
    if (attempt.acquired) assert.deepEqual(await child.closed, [0, null], child.stderr);
    manifest.dsh.profile.bundles = manifest.dsh.profile.bundles.filter(name => name !== 'other-plugin');
    await writeFileAtomic(f.file, JSON.stringify(manifest), { mode: 0o600 });
  }, { waitMs: 0 });
  assert.deepEqual(await child.closed, [0, null], child.stderr);
  assert.equal(child.stderr, '');
  const finalBundles = JSON.parse(fs.readFileSync(f.file, 'utf8')).dsh.profile.bundles;
  t.diagnostic(JSON.stringify({ schedule: 'actual fs read/wx boundary', ...observed, finalBundles }));
  assert.deepEqual({ ...observed, finalBundles, locks: lockNames(f.file) }, {
    attempt: { acquired: false, code: 'EEXIST' },
    liveRecord: process.pid + '\n',
    finalBundles: ['@deepseek-ai/dsh-base'],
    locks: [],
  });
});
