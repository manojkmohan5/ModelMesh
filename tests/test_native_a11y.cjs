const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const web = file => path.join(__dirname, '../native/web', file);
function load(file, dependencies = {}) {
  let code = ts.transpileModule(fs.readFileSync(web(file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  for (const [specifier, url] of Object.entries(dependencies)) code = code.replace(JSON.stringify(specifier), JSON.stringify(url));
  return import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
}
function bundle(entry) {
  const code = require('esbuild').buildSync({
    entryPoints: [web(entry)], bundle: true, write: false, format: 'esm', platform: 'node',
  }).outputFiles[0].text;
  return import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
}

test('turn transitions announce only when work finishes, waits or stops', async () => {
  const { turnAnnouncement } = await load('turn-announcement.ts');
  assert.equal(turnAnnouncement('running', 'idle'), 'Reply ready');
  assert.equal(turnAnnouncement('saving', 'idle'), 'Reply ready');
  assert.equal(turnAnnouncement('running', 'waiting'), 'Needs your input');
  assert.equal(turnAnnouncement('stopping', 'stopped'), 'Stopped');
  assert.equal(turnAnnouncement('running', 'failed'), 'Failed');
  assert.equal(turnAnnouncement('waiting', 'running'), '');
  // Opening a finished conversation, or a status that did not change, is silent.
  assert.equal(turnAnnouncement(undefined, 'idle'), '');
  assert.equal(turnAnnouncement('idle', 'idle'), '');
  assert.equal(turnAnnouncement('idle', 'failed'), '');
});

test('date errors name the field they are about', async () => {
  const dates = await bundle('analytics-dates.ts');
  const fails = (start, through) => { try { dates.customAnalyticsRange(start, through); } catch (error) { return error; } };
  assert.deepEqual([fails('2026-13-01', '2026-01-02').field, fails('2026-13-01', '2026-01-02').message], ['start', 'From: Choose a valid calendar date.']);
  assert.equal(fails('2026-01-01', 'soon').field, 'through');
  assert.match(fails('2026-01-01', 'soon').message, /^Through: /);
  const order = fails('2026-02-01', '2026-01-01');
  assert.equal(order.field, 'through');
  assert.match(order.message, /^Through: .*From/);
  assert.equal(fails('2026-01-01', '2026-01-31'), undefined);
});

test('partial-coverage bars say so in their title', async () => {
  const { analyticsChart } = await bundle('analytics-render.js');
  const series = [{ key: 'cost', label: 'Model' }];
  const markup = analyticsChart([{ date: '2026-01-01', cost: 2, partial: true }, { date: '2026-01-02', cost: 3 }], series, { title: 'Spend', money: true });
  assert.match(markup, /Model: \$2[^<]*\(partial coverage\)<\/title>/);
  assert.doesNotMatch(markup, /Model: \$3[^<]*partial/);
});
