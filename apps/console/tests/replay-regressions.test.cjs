const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');

function load(file, data, extra = '', overrides = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../src', file), 'utf8') + extra;
  const code = ts.transpileModule(source, {compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS}}).outputText;
  const exports = {};
  const wrapper = ({children}) => React.createElement('div', null, children);
  const ui = new Proxy({useAsync:()=>({data,loading:false,error:null,reload(){}}),pct:()=> '0%',ago:()=> 'now',DECISION_TEXT:{ALLOW:'Allowed',BLOCK:'Blocked',ESCALATE:'Needs approval',NEUTRALISE:'Neutralised'},Stat:({value,label})=>React.createElement('div',null,value,label)}, {get:(o,k)=>o[k] ?? wrapper});
  vm.runInNewContext(code,{exports,require:n=>overrides[n] ?? (n==='react'?React:n==='react/jsx-runtime'?require(n):n.endsWith('/ui')?ui:n.endsWith('/reviewer')?{useReviewer:()=>null}:n.endsWith('/ReviewerSignIn')?{ReviewerSignIn:({label})=>React.createElement('button',null,label ?? 'Reviewer sign-in')}:{api:{},admin:{}})});
  return exports;
}

test('public metrics with withheld memory do not crash or invent zero counts',()=>{
  const m={decisions:1,inspections:{n:1,escalation_rate:0,escalated_to_judge:0,degraded:0},latency_ms:{fast_path:{p95:1},judge_path:{p95:null}},by_decision:{ALLOW:1},by_rule:{allow:1},providers:[],memory:null,approvals_pending:0};
  const {LiveOperations}=load('components/LivePanels.tsx',m);
  const html=renderToStaticMarkup(React.createElement(LiveOperations));
  assert.match(html,/Memory counts are restricted/);
  assert.doesNotMatch(html,/Confirmed attacks.*0/);
});

test('allowed low-score classifier observations are not called flagged content',()=>{
  const {SourceStep}=load('components/Provenance.tsx',null,'\nexport { SourceStep };');
  const s={source:'tool:terminal',seq:13,event_id:'e13',decision:'ALLOW',proven:true,link:'argument path copies text',labels:['Indirect Prompt Injection'],evidence:[{signal:'classifier',score:0.0687,span:'meeting-notes.txt'}]};
  const html=renderToStaticMarkup(React.createElement(SourceStep,{s}));
  assert.match(html,/Inspected content/);
  assert.doesNotMatch(html,/Flagged content/);
  const blocked=renderToStaticMarkup(React.createElement(SourceStep,{s:{...s,decision:'BLOCK'}}));
  assert.match(blocked,/Flagged content/);
});

test('Investigator runs without a sign-in; proposals still wait for a reviewer',()=>{
  const {InvestigationPanel}=load('components/Investigation.tsx',[]);
  const html=renderToStaticMarkup(React.createElement(InvestigationPanel,{sessionId:'demo'}));
  assert.match(html,/<button[^>]*>Investigate this session<\/button>/);
  assert.doesNotMatch(html,/Sign in/);
  assert.match(html,/waits for a reviewer/);
});
