export default function ({parentElement, data, setTriggerValue}) {
  const shell = parentElement.querySelector('.composer-shell');
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
  shell.classList.toggle('docked', data.docked);
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
  chips.replaceChildren();
  for (const [label, task, prompt] of data.tasks) {
    const button = document.createElement('button');
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
    chips.appendChild(button);
  }
  const submit = () => {
    const text = input.value.trim();
    if (busy || !text) return;
    busy = true;
    input.value = '';
    shell.classList.add('docked');
    update();
    setTriggerValue('submitted', {text, task: shell.task});
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
  return () => observer.disconnect();
}
