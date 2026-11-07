const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../native/web/model-choice.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const choice = import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));

function useStorage(storage) {
  Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true, writable: true });
}
const memory = () => {
  const values = new Map();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
};
const blocked = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };

test('the Auto row comes first and only when the server offers smart routing', async () => {
  const { modelOptions } = await choice;
  assert.deepEqual(modelOptions(['glm-5.3', 'beam-1.0'], 'auto'), [
    { value: 'auto', label: 'Auto (smart routing)' }, { value: 'glm-5.3', label: 'glm-5.3' }, { value: 'beam-1.0', label: 'beam-1.0' },
  ]);
  for (const auto of [null, undefined]) {
    assert.deepEqual(modelOptions(['openai/x'], auto, name => name.replace(/^openai\//, '')), [{ value: 'openai/x', label: 'x' }]);
  }
});

test('Auto is preselected until the user picks a fixed model, and only while it is offered', async () => {
  const { rememberAuto, startingModel, wantsAuto } = await choice;
  useStorage(memory());
  assert.equal(wantsAuto(), true);
  assert.equal(startingModel('glm-5.3', 'auto'), 'auto');
  assert.equal(startingModel('glm-5.3', null), 'glm-5.3');
  rememberAuto(false);
  assert.equal(startingModel('glm-5.3', 'auto'), 'glm-5.3');
  rememberAuto(true);
  assert.equal(startingModel('glm-5.3', 'auto'), 'auto');
});

test('blocked browser storage never breaks the picker and keeps the conversation model', async () => {
  const { rememberAuto, startingModel, wantsAuto } = await choice;
  useStorage(blocked);
  assert.doesNotThrow(() => rememberAuto(true));
  assert.equal(wantsAuto(), false);
  assert.equal(startingModel('glm-5.3', 'auto'), 'glm-5.3');
});
