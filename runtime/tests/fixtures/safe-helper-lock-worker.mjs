import fs from 'node:fs';
import { createRequire } from 'node:module';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [mode, filename, helper, gate] = process.argv.slice(2);
if (mode === 'holder') {
  const { withFileLock } = await import(pathToFileURL(join(resolve(process.env.DSH_PREFIX),
    'node_modules/@deepseek-ai/dsh-atomic-write/lib/index.js')));
  process.on('message', () => {});
  await withFileLock(filename, async () => {
    process.send({ type: 'held' });
    await new Promise(() => {});
  }, { waitMs: 0 });
} else {
  const lock = filename + '.lock';
  const read = fs.readFileSync, write = fs.writeFileSync;
  const pause = new Int32Array(new SharedArrayBuffer(4));
  let captured = false, attempts = 0;
  function report(kind, value) {
    const file = gate + '.' + kind;
    write(file + '.tmp', JSON.stringify(value));
    fs.renameSync(file + '.tmp', file);
  }
  // Pause after the actual read, preserving its exact result. The parent can
  // take over the dead lock while this helper still holds the old record.
  fs.readFileSync = function (file, ...args) {
    const value = read(file, ...args);
    if (file === lock && !captured) {
      captured = true;
      report('read', { record: String(value) });
      const deadline = Date.now() + 4000;
      while (!fs.existsSync(gate + '.go')) {
        if (Date.now() >= deadline) throw new Error('fixture: parent did not release old lock read');
        Atomics.wait(pause, 0, 0, 10);
      }
    }
    return value;
  };
  // Observe the next real main-lock attempt; never substitute its result or
  // touch the production library's fs/promises calls in the parent process.
  fs.writeFileSync = function (file, ...args) {
    if (file !== lock) return write(file, ...args);
    attempts++;
    try {
      const value = write(file, ...args);
      if (attempts === 2) report('attempt', { acquired: true });
      return value;
    } catch (error) {
      if (attempts === 2) report('attempt', { acquired: false, code: error.code });
      throw error;
    }
  };
  process.argv = [process.execPath, helper, 'disable', 'helper-plugin'];
  createRequire(import.meta.url)(helper);
  process.disconnect();
}
