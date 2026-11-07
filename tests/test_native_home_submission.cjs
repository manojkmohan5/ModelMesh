// Source verification only; no browser or shared preview is controlled.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const ts=require('typescript');
const root=path.resolve(__dirname,'..');
let homeSubmission;
test.before(async()=>{
  // Import the actual pure admission helper without evaluating the home canvas bootstrap.
  const source=ts.createSourceFile('home.tsx',fs.readFileSync(path.join(root,'native/web/home.tsx'),'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  const fn=source.statements.find(node=>ts.isFunctionDeclaration(node)&&node.name?.text==='homeSubmission');assert.ok(fn);
  const code=ts.transpileModule(fn.getText(source),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText;
  ({homeSubmission}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64')));
});

test('Home first message sends only prompt, model and attachments with a stable retry identity',()=>{
  const result=homeSubmission('Task','model',true,['image']);
  assert.deepEqual(result.body,{prompt:'Task',model:'model',attachments:['image']});assert.equal(result.visibility,'private');
  assert.equal(result.signature,homeSubmission('Task','model',true,['image']).signature);
  assert.notEqual(result.signature,homeSubmission('Task','model',false,['image']).signature);
  assert.equal(homeSubmission('Task','model',false,[]).visibility,'shared');
});
