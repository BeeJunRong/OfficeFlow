export default function ({parentElement, data, setTriggerValue}) {
  const shell = parentElement.querySelector('.composer-shell');
  // Isolated components render inside a shadow root; the host element is what
  // the outer page can select. Marking it lets the page dock the component as
  // the last flex item without reaching into the shadow tree.
  const host = parentElement.host || parentElement;

  const mainColumn = () =>
    host.closest ? host.closest('[data-testid="stMainBlockContainer"]') : null;

  const scrollToBottom = () => {
    const column = mainColumn();
    if (column) column.scrollTop = column.scrollHeight;
  };

  const nearBottom = () => {
    const column = mainColumn();
    if (!column) return false;
    return column.scrollHeight - column.scrollTop - column.clientHeight < 200;
  };

  const placeWelcome = () => {
    const column = mainColumn();
    if (!column) return;  // Node.js unit-test mock has no layout.
    if (shell.classList.contains('docked')) return;
    const rect = column.getBoundingClientRect();
    if (!rect.width) return;
    // Keep the welcome composer centered on the conversation column, not on
    // the whole window: the sidebar can be expanded or collapsed.
    shell.style.left = `${rect.left + rect.width / 2}px`;
    shell.style.width = `${Math.min(720, Math.max(0, rect.width - 32))}px`;
  };

  const setDocked = value => {
    shell.classList.toggle('docked', value);
    if (host && host.dataset) {
      host.dataset.docked = value ? 'true' : 'false';
    }
    if (value) {
      // Docked layout is pure CSS (static, 100% of the column); clear the
      // welcome-position inline styles so they cannot override it.
      if (shell.style) {
        shell.style.left = '';
        shell.style.width = '';
      }
      scrollToBottom();
    } else {
      placeWelcome();
    }
  };

  const input = shell.querySelector('textarea');
  const send = shell.querySelector('.send');
  const chips = shell.querySelector('.task-chips');
  const hint = shell.querySelector('.composer-hint');
  // Preserve the local draft across unrelated Streamlit reruns.
  if (shell.chatEpoch !== data.epoch) {
    input.value = '';
    shell.task = data.task;
    shell.chatEpoch = data.epoch;
  }
  // A rerun may acknowledge submission while the local transition is running.
  // Only a new conversation may return this same node to the welcome position.
  setDocked(Boolean(data.docked));
  let busy = data.busy;
  const update = () => {
    input.style.height = 'auto';
    input.style.height = `${input.scrollHeight}px`;
    send.disabled = busy || !input.value.trim();
    input.disabled = busy;
    hint.textContent = busy ? '正在整理，请稍候…' : 'Enter 发送 · Shift + Enter 换行';
    shell.querySelectorAll('.chip').forEach(button => {
      button.disabled = busy;
      button.setAttribute('aria-pressed', String(button.dataset.task === (shell.task || '')));
    });
  };
  for (const [index, [label, task, prompt]] of data.tasks.entries()) {
    const button = chips.children[index] || document.createElement('button');
    button.type = 'button';
    button.className = 'chip';
    button.textContent = label;
    button.dataset.task = task || '';
    button.onclick = () => {
      shell.task = task;
      // Selecting a skill never overwrites material the user already entered.
      if (!input.value.trim() && prompt) input.value = prompt;
      update();
      input.focus();
    };
    if (!button.parentNode) chips.appendChild(button);
  }
  let transitionFrame;
  let notifyFrame;
  const submit = () => {
    const text = input.value.trim();
    if (busy || !text) return;
    busy = true;
    input.value = '';
    update();
    // Paint the cleared, still-mounted input before changing position and
    // notifying Python. The backend rerun must not precede this local update.
    transitionFrame = requestAnimationFrame(() => {
      setDocked(true);
      notifyFrame = requestAnimationFrame(() => {
        setTriggerValue('submitted', {text, task: shell.task});
      });
    });
  };
  input.oninput = update;
  input.onkeydown = event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      submit();
    }
  };
  send.onclick = submit;
  update();
  const observer = new ResizeObserver(() => {
    document.documentElement.style.setProperty('--of-composer-height', `${shell.offsetHeight + 24}px`);
  });
  observer.observe(shell);

  // The sidebar toggle changes the column's position (and often the main
  // area's width) without a window resize event. Observe both so the welcome
  // overlay follows the conversation column.
  const column = mainColumn();
  const main = host.closest ? host.closest('[data-testid="stMain"]') : null;
  const columnObserver = column && typeof ResizeObserver !== 'undefined'
    ? new ResizeObserver(placeWelcome)
    : null;
  const mainObserver = main && typeof ResizeObserver !== 'undefined'
    ? new ResizeObserver(placeWelcome)
    : null;
  // Keep the newest message above the docked composer as the agent streams
  // in, but never yank the viewport if the user has scrolled up to read.
  const content = column && column.firstElementChild;
  const contentObserver = content && typeof ResizeObserver !== 'undefined'
    ? new ResizeObserver(() => { if (nearBottom()) scrollToBottom(); })
    : null;
  if (columnObserver) columnObserver.observe(column);
  if (mainObserver) mainObserver.observe(main);
  if (contentObserver) contentObserver.observe(content);
  if (typeof window !== 'undefined') window.addEventListener('resize', placeWelcome);
  placeWelcome();

  return () => {
    observer.disconnect();
    if (columnObserver) columnObserver.disconnect();
    if (mainObserver) mainObserver.disconnect();
    if (contentObserver) contentObserver.disconnect();
    if (typeof window !== 'undefined') window.removeEventListener('resize', placeWelcome);
    cancelAnimationFrame(transitionFrame);
    cancelAnimationFrame(notifyFrame);
  };
}
