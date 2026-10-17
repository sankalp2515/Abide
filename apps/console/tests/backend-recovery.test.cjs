const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

// Run the real hook/effect code with a controlled clock and React hook boundary.
// This keeps cold-start checks fast without a server or added test dependencies.
function harness() {
  let cursor = 0, now = 0;
  const slots = [], effects = [], timers = new Map();
  const react = {
    createContext: value => ({ value }),
    useContext: context => context.value,
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }];
    },
    useRef(value) { const i = cursor++; return slots[i] ??= { current: value }; },
    useEffect(effect, deps) {
      const i = cursor++, previous = slots[i];
      if (!previous || deps.some((value, j) => !Object.is(value, previous.deps[j]))) {
        effects.push(() => { previous?.cleanup?.(); slots[i] = { deps, cleanup: effect() }; });
      }
    },
  };
  const jsx = (type, props) => ({ type, props });
  const api = { health: async () => { throw new ApiError(0, 'sleeping'); } };
  class ApiError extends Error { constructor(status, message) { super(message); this.status = status; } }
  function load(file, extra = '', imports = {}) {
    const source = fs.readFileSync(path.join(__dirname, '../src', file), 'utf8').replaceAll('import.meta.env.BASE_URL', '"/"') + extra;
    const code = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } }).outputText;
    const exports = {};
    vm.runInNewContext(code, {
      exports, require: name => {
        if (imports[name]) return imports[name];
        if (name === 'react') return react;
        if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
        if (name.endsWith('/api')) return { api, ApiError, PRODUCT_NAME: 'Abide', AGENT: 'delegate-workspace' };
        return {};
      },
      Date: { now: () => now },
      setInterval: callback => { const id = {}; timers.set(id, callback); return id; },
      clearInterval: id => timers.delete(id),
      document: { hidden: false },
    });
    return exports;
  }
  return {
    api, ApiError, load, timers,
    render(fn) { cursor = 0; const result = fn(); effects.splice(0).forEach(effect => effect()); return result; },
    async tick(time) { now = time; await Promise.all([...timers.values()].map(fn => fn())); },
    unmount() { slots.forEach(slot => slot?.cleanup?.()); },
  };
}

function text(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node !== 'object') return String(node);
  if (Array.isArray(node)) return node.map(text).join(' ');
  if (typeof node.type === 'function') return text(node.type(node.props));
  return text(node.props?.children);
}

for (const statusCode of [0, 503]) {
  test(`cold start (${statusCode}) stays waking for 150 seconds and resets after recovery`, async () => {
    const h = harness();
    h.api.health = async () => { throw new h.ApiError(statusCode, 'sleeping'); };
    const { testHook } = h.load('components/Layout.tsx', '\nexport { useBackendStatus as testHook };');
    const status = () => h.render(testHook);
    status(); await h.tick(0);
    assert.equal(status(), 'waking');
    await h.tick(30000); assert.equal(status(), 'waking');
    await h.tick(149999); assert.equal(status(), 'waking');
    await h.tick(150000); assert.equal(status(), 'down');
    h.api.health = async () => ({ ok: true });
    await h.tick(165000); assert.equal(status(), 'ok');
    h.api.health = async () => { throw new h.ApiError(0, 'sleeping again'); };
    await h.tick(180000); assert.equal(status(), 'waking');
    await h.tick(300000); assert.equal(status(), 'waking');
    h.unmount(); assert.equal(h.timers.size, 0);
  });
}

test('Overview refetches an empty successful scorecard when health recovers, once per transition', () => {
  const h = harness();
  let reloads = 0;
  const reload = () => { reloads++; };
  const context = { value: 'waking' };
  const { Overview } = h.load('pages/Overview.tsx', '', {
    '../components/Layout': { useBackendAvailability: () => context.value },
    '../components/ui': { useAsync: () => ({ data: null, error: null, loading: false, reload }) },
  });
  h.render(Overview); assert.equal(reloads, 0);
  context.value = 'ok'; h.render(Overview); assert.equal(reloads, 1);
  h.render(Overview); assert.equal(reloads, 1);
  context.value = 'waking'; h.render(Overview);
  context.value = 'ok'; h.render(Overview); assert.equal(reloads, 2);
});

test('Overview keeps waking copy instead of unavailable copy while the backend starts', () => {
  const h = harness();
  let status = 'waking';
  const { Overview } = h.load('pages/Overview.tsx', '', {
    '../components/Layout': { useBackendAvailability: () => status },
    '../components/ui': { useAsync: () => ({ data: null, error: 'unreachable', loading: false, reload() {} }), pct: () => '—' },
  });
  const content = text(h.render(Overview));
  assert.match(content, /waking up/i);
  assert.doesNotMatch(content, /service (?:is )?unavailable/i);
  status = 'down';
  assert.match(text(h.render(Overview)), /service (?:is )?unavailable/i);
});

test('slow health requests do not overlap or update status after unmount', async () => {
  const h = harness();
  let finish, calls = 0;
  h.api.health = () => { calls++; return new Promise(resolve => { finish = resolve; }); };
  const { testHook } = h.load('components/Layout.tsx', '\nexport { useBackendStatus as testHook };');
  assert.equal(h.render(testHook), 'checking');
  await h.tick(15000); await h.tick(30000);
  assert.equal(calls, 1);
  h.unmount(); finish({ ok: true }); await Promise.resolve();
  assert.equal(h.render(testHook), 'checking');
  assert.equal(h.timers.size, 0);
});
