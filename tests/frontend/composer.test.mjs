import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const source = readFileSync(new URL('../../frontend/composer_assets/composer.js', import.meta.url), 'utf8');
const {default: render} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('clears before notifying Python, keeps nodes and docking across consecutive sends', () => {
  const frames = new Map();
  let frameId = 0;
  globalThis.requestAnimationFrame = fn => { frames.set(++frameId, fn); return frameId; };
  globalThis.cancelAnimationFrame = id => frames.delete(id);
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  globalThis.document = {
    documentElement: {style: {setProperty() {}}},
    createElement: () => ({dataset: {}, setAttribute() {}, parentNode: null}),
  };
  const input = {value: '', style: {}, scrollHeight: 56, focus() {}};
  const send = {};
  const chips = {children: [], appendChild(button) {button.parentNode = this; this.children.push(button);}};
  const hint = {};
  const classes = new Set();
  const shell = {
    classList: {add: c => classes.add(c), toggle: (c, value) => value ? classes.add(c) : classes.delete(c)},
    querySelector: selector => ({textarea: input, '.send': send, '.task-chips': chips, '.composer-hint': hint})[selector],
    querySelectorAll: () => chips.children,
  };
  const data = {epoch: 0, task: null, docked: false, busy: false, tasks: [['自动识别', null, '']]};
  const events = [];
  // Host marker is what the outer page uses to dock the component as a flex item.
  const parentElement = {querySelector: () => shell, dataset: {}};
  const mount = () => render({parentElement, data: {...data}, setTriggerValue: (_, value) => events.push(value)});
  const flush = () => {const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(fn => fn());};
  let cleanup = mount();
  const originalChip = chips.children[0];
  for (const text of ['第一条消息', '第二条消息', '第三条消息']) {
    input.value = text;
    input.oninput();
    const before = events.length;
    send.onclick();
    assert.equal(input.value, '');
    assert.equal(events.length, before);
    assert.equal(send.disabled, true);
    flush();
    assert.equal(classes.has('docked'), true);
    assert.equal(parentElement.dataset.docked, 'true');
    assert.equal(events.length, before);
    flush();
    assert.equal(events.at(-1).text, text);
    cleanup();
    data.busy = true;
    data.docked = true;
    cleanup = mount();
    assert.equal(chips.children[0], originalChip);
    assert.equal(classes.has('docked'), true);
    cleanup();
    data.busy = false;
    cleanup = mount();
    assert.equal(input.disabled, false);
  }
  input.value = '未发送的草稿';
  cleanup();
  cleanup = mount();
  assert.equal(input.value, '未发送的草稿');
  data.epoch++;
  data.docked = false;
  cleanup();
  cleanup = mount();
  assert.equal(classes.has('docked'), false);
  assert.equal(parentElement.dataset.docked, 'false');
  assert.equal(input.value, '');
  cleanup();
});
