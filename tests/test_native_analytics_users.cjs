// Focused production-data/React regressions. Fixtures are supplementary;
// parent qualifies the same report in the actual deployed app.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');

const root = path.resolve(__dirname, '..');
const cache = new Map();
function load(relative) {
  let filename = path.isAbsolute(relative) ? relative : path.join(root, relative);
  if (!path.extname(filename)) filename = ['.ts', '.tsx'].map(ext => filename + ext).find(fs.existsSync);
  if (!filename) throw new Error(`Missing production source: ${relative}`);
  if (cache.has(filename)) return cache.get(filename).exports;
  const module = {exports: {}};
  cache.set(filename, module);
  const requireFromFile = createRequire(filename);
  const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true},
  }).outputText;
  const scopedRequire = request => {
    if (request.endsWith('.css')) return {};
    // UsersReport does not render charts/date controls. Their Vite-only legacy
    // adapter is outside this test; table/Input/Button are the real shared components.
    if (request === './analytics-shared' || request === './analytics-render.js') return {};
    // The generated SDK only fetches reports, which UsersReport receives as props.
    if (request === './generated/api') return {};
    if (request.startsWith('@/')) return load(path.join(root, 'frontend', request.slice(2)));
    if (request.startsWith('.')) return load(path.resolve(path.dirname(filename), request));
    return requireFromFile(request);
  };
  new Function('require', 'module', 'exports', '__filename', '__dirname', compiled)(
    scopedRequire, module, module.exports, filename, path.dirname(filename),
  );
  return module.exports;
}
const {combinedUserRows, sortUserRows} = load('native/web/analytics-data.ts');
const {UsersReport} = load('native/web/analytics.tsx');
const person = (id, display_name = id, username = id) => ({id, display_name, username});
function report(by_user, users = []) {
  return {
    by_user, users, summary: {}, rows: [], series: [], models: [],
    capabilities: {reported_costs: true},
    methodology: {cost: 'Only authoritative response amounts are included.'},
  };
}
function render(data, search = '', sort = {key: 'spend', direction: 'desc'}) {
  return renderToStaticMarkup(React.createElement(UsersReport, {
    data, search, sort, onSearch() {}, onSort() {},
  }));
}
const text = html => html.replace(/<[^>]*>/g, '').replace(/&amp;/g, '&').replace(/&#x27;/g, "'");
const cells = html => [...html.matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/g)].map(match => text(match[1]));
const ids = rows => rows.map(row => row.user_id);

test('exact-ID union retains spend-only and activity-only users without directory-only rows', () => {
  const input = [
    {user_id: 'a', sessions: 2, turns: 3, requests: 4, cost_usd: 1.25, pricing_status: 'partial'},
    {user_id: 'a', sessions_created: 2},
    {user_id: 'spend-only', cost_usd: 0, requests: 1},
    {user_id: 'activity-only', turns: 1},
  ];
  const snapshot = structuredClone(input);
  const rows = combinedUserRows(input, [person('a', 'Alice'), person('unused', 'Directory only')]);
  assert.deepEqual(ids(rows), ['a', 'spend-only', 'activity-only']);
  assert.equal(rows[0].label, 'Alice');
  assert.equal(rows[0].metrics.cost_usd, 1.25);
  assert.equal(rows[0].metrics.sessions_created, 2);
  assert.equal(rows[2].metrics.cost_usd, undefined, 'Absent spend is not fabricated as zero');
  assert.equal(rows[1].metrics.turns, undefined, 'Missing activity observations stay unknown');
  assert.deepEqual(input, snapshot, 'Projection never mutates report aggregates');
});

test('same display names and case-different IDs remain separate people', () => {
  const rows = combinedUserRows([{user_id: 'User-A', cost_usd: 3}, {user_id: 'user-a', turns: 7}], [
    person('User-A', 'Same name', 'first'), person('user-a', 'Same name', 'second'),
  ]);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map(row => row.secondary), ['first', 'second']);
  assert.equal(rows[0].metrics.turns, undefined);
  assert.equal(rows[1].metrics.cost_usd, undefined);
});

test('undefined projection fields do not erase observations; explicit unknown remains unknown', () => {
  const rows = combinedUserRows([
    {user_id: 'a', cost_usd: null, total_tokens: null},
    {user_id: 'a', cost_usd: undefined, turns: 1},
  ], []);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].metrics.cost_usd, null);
  assert.equal(rows[0].metrics.total_tokens, null);
  assert.equal(rows[0].metrics.turns, 1);
});

test('reported zero sorts as data and unknown values remain last in both directions', () => {
  const rows = combinedUserRows([
    {user_id: 'unknown', cost_usd: null}, {user_id: 'zero', cost_usd: 0},
    {user_id: 'paid', cost_usd: 10}, {user_id: 'invalid', cost_usd: Infinity},
  ], []);
  const asc = ids(sortUserRows(rows, {key: 'spend', direction: 'asc'}));
  const desc = ids(sortUserRows(rows, {key: 'spend', direction: 'desc'}));
  assert.deepEqual(asc.slice(0, 2), ['zero', 'paid']);
  assert.deepEqual(desc.slice(0, 2), ['paid', 'zero']);
  assert.deepEqual(new Set(asc.slice(2)), new Set(['unknown', 'invalid']));
  assert.deepEqual(ids(rows), ['unknown', 'zero', 'paid', 'invalid']);
});

test('single table has every requested metric, one row per identity, and one sort context', () => {
  const data = report([
    {user_id: 'a', sessions_active: 2, turns: 3, requests: 4, total_tokens: 500, cost_usd: 1.25, pricing_status: 'partial'},
    {user_id: 'b', turns: 0},
  ], [person('a', 'Alice'), person('b', 'Bob')]);
  const html = render(data);
  assert.equal((html.match(/<table\b/g) || []).length, 1);
  assert.equal((html.match(/<tr\b/g) || []).length, 3, 'One header plus two users');
  const headers = [...html.matchAll(/<th\b[^>]*>([\s\S]*?)<\/th>/g)].map(match => text(match[1]));
  assert.deepEqual(headers, ['User', 'Sessions', 'Turns', 'Requests', 'Tokens', 'Reported spend']);
  assert.match(html, /aria-label="Usage by user" tabindex="0"/);
  assert.equal((html.match(/aria-sort="descending"/g) || []).length, 1);
  assert.match(html, /Partial/);
  assert.doesNotMatch(html, /Model usage by user|PRs created|merged PR/);
});

test('methodology remains available but collapsed once, outside the compact row', () => {
  const html = render(report([{user_id: 'a', cost_usd: null}], [person('a', 'Alice')]));
  assert.equal((html.match(/<details\b/g) || []).length, 1);
  assert.doesNotMatch(html, /<details\b[^>]*\bopen(?:=|>|\s)/);
  assert.match(html, /Coverage and calculations/);
  assert.doesNotMatch(html, /Only authoritative response amounts are included/);
  assert.match(html, /Dates use UTC/);
});

test('one search filters every column by genuine identity fields', () => {
  const data = report([{user_id: 'a', cost_usd: 1}, {user_id: 'b', turns: 2}], [
    person('a', 'Alice', 'alice', 'alice@example.com'), person('b', 'Bob', 'bob'),
  ]);
  const html = render(data, 'BOB');
  assert.equal((html.match(/<tr\b/g) || []).length, 2);
  assert.match(html, /<strong>Bob<\/strong>/);
  assert.doesNotMatch(html, /<strong>Alice<\/strong>/);
  assert.match(html, /1 of 2 users/);
  const empty = render(data, 'not-present');
  assert.match(empty, /No matching users/);
  assert.match(empty, />Clear<\/button>/);
  assert.doesNotMatch(empty, /<table\b/);
});

test('missing spend data stays unavailable, with no invented free cost', () => {
  const data = report([{user_id: 'a', requests: 0, unknown_request_turns: 1, total_tokens: null, cost_usd: null,
    pricing_status: 'unpriced'}], [person('a', 'Alice')]);
  const values = cells(render(data));
  assert.equal(values.length, 6);
  assert.match(values[3], /0.*1 unknown turn/);
  assert.equal(values[4], 'Unavailable');
  assert.equal(values[5], 'Unavailable');
  assert.doesNotMatch(render(data), /Unavailable in|Unavailable out/);
});
