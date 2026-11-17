// Presentation adapter: keep the original selects as the settings source.
(() => {
  // Shared placement for every picker menu: follow the button width, grow to
  // fit long option text, stay inside the viewport, and flip upwards when the
  // space below is too small (same behaviour as the device menu).
  function placePickerMenu(button, menu, minWidth = 0) {
    if (!button || !menu || menu.hidden) return;
    const margin = 8;
    // 选项菜单按内容取宽；auto 会被 grid 的 1fr 压缩导致“NVIDIA GPU”“快速 · 384”换行。
    // 模型菜单有固定最小宽度和等宽行，继续按按钮宽度测量。
    menu.style.width = minWidth ? 'auto' : 'max-content';
    menu.style.maxHeight = 'none';
    const natural = menu.scrollWidth;
    const box = button.getBoundingClientRect();
    const width = Math.max(0, Math.min(
      Math.max(box.width, minWidth, natural + 2),
      innerWidth - margin * 2,
    ));
    menu.style.width = width + 'px';
    const naturalHeight = Math.min(220, menu.scrollHeight);
    const spaceBelow = innerHeight - box.bottom - margin;
    const spaceAbove = box.top - margin;
    const openUp = naturalHeight > spaceBelow && spaceAbove > spaceBelow;
    const available = openUp ? spaceAbove : spaceBelow;
    const height = Math.max(72, Math.min(naturalHeight, available));
    // 边框不计入 scrollHeight，补 2px 避免全部选项已放下时仍出现滚动条。
    menu.style.maxHeight = (height + 2) + 'px';
    const top = openUp
      ? Math.max(margin, box.top - height - 4)
      : Math.min(box.bottom + 4, innerHeight - height - margin);
    const left = Math.max(margin, Math.min(box.left, innerWidth - width - margin));
    menu.style.top = top + 'px';
    menu.style.left = left + 'px';
    menu.dataset.direction = openUp ? 'up' : 'down';
  }
  window.placePickerMenu = placePickerMenu;

  function attach(select, className) {
    if (!select) return null;
    const base = select.id || 'picker';
    const buttonId = (base.endsWith('Select') ? base.slice(0, -6) : base) + 'Btn';
    const menuId = buttonId + 'Menu';
    const picker = document.createElement('div');
    picker.className = 'model-picker ' + className;
    picker.innerHTML = '<button type="button" id="' + buttonId + '" class="model-btn" aria-haspopup="listbox" aria-expanded="false" aria-controls="' + menuId + '"><span></span><i class="caret" aria-hidden="true"></i></button><div id="' + menuId + '" class="model-menu" role="listbox" hidden></div>';
    select.after(picker);
    select.hidden = true;
    const label = select.id ? document.querySelector('label[for="' + select.id + '"]') : null;
    if (label) label.htmlFor = buttonId;
    const button = picker.querySelector('button');
    const menu = picker.querySelector('[role="listbox"]');
    menu.setAttribute('aria-label', select.getAttribute('aria-label') || label?.textContent || select.title || '选项');
    button.setAttribute('aria-label', menu.getAttribute('aria-label'));
    if (select.dataset.tip) button.dataset.tip = select.dataset.tip;

    function position() {
      placePickerMenu(button, menu, select.id === 'deviceSelect' ? 300 : 0);
    }
    function close(restoreFocus = false) {
      menu.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      if (restoreFocus && !button.disabled) button.focus();
    }
    function sync() {
      const selected = select.selectedOptions[0];
      const span = button.querySelector('span');
      span.textContent = selected?.dataset.buttonLabel || selected?.textContent || '';
      if (select.id === 'deviceSelect') {
        const state = document.querySelector('#gpuState');
        if (state && !state.hidden && state.dataset.tip) button.dataset.tip = state.dataset.tip;
        else button.removeAttribute('data-tip');
      }
      button.disabled = select.disabled;
      for (const option of menu.querySelectorAll('[data-value]')) {
        option.setAttribute('aria-selected', String(option.dataset.value === select.value));
      }
      if (select.disabled) close();
      syncGpu();
    }
    function syncGpu() {
      // Mirror the GPU action button so the device menu shows it after the
      // "GPU (CUDA)" name, exactly like the model menus.
      const source = document.querySelector('#gpuDownload');
      if (!source) return;
      const box = document.querySelector('#gpuState');
      const gone = source.hidden || (box && box.hidden);
      const ready = box && !box.hidden && box.classList.contains('ready');
      const note = document.querySelector('#gpuStateText');
      for (const description of menu.querySelectorAll('[data-device-description="cuda"]')) {
        description.textContent = box && !box.hidden && note?.textContent
          ? note.textContent.replace(/^当前为\s*/, '') : '使用 NVIDIA GPU 加速';
      }
      for (const status of menu.querySelectorAll('[data-gpu-ready]')) status.hidden = !ready;
      for (const action of menu.querySelectorAll('[data-gpu]')) {
        action.textContent = source.textContent || '下载 GPU 环境';
        action.disabled = source.disabled;
        action.hidden = gone;
        if (source.dataset.tip) action.dataset.tip = source.dataset.tip;
        else action.removeAttribute('data-tip');
      }
    }
    function open(focusOption = false) {
      if (select.disabled) return;
      menu.replaceChildren(...Array.from(select.options, option => {
        const row = document.createElement('div');
        row.className = 'model-choice';
        const item = document.createElement('button');
        item.type = 'button'; item.className = 'model-option';
        item.setAttribute('role', 'option'); item.tabIndex = -1;
        item.dataset.value = option.value;
        if (select.id === 'deviceSelect') {
          const name = document.createElement('span');
          name.className = 'model-name'; name.textContent = option.textContent;
          const description = document.createElement('small');
          description.className = 'model-desc';
          description.dataset.deviceDescription = option.value;
          description.textContent = option.value === 'auto' ? '优先使用可用 GPU，否则使用 CPU'
            : option.value === 'cpu' ? '使用 CPU 运行' : '使用 NVIDIA GPU 加速';
          item.append(name, description);
        } else item.textContent = option.textContent;
        item.disabled = option.disabled;
        row.append(item);
        if (option.value === 'cuda' && select.id === 'deviceSelect') {
          row.classList.add('gpu-choice');
          const gpu = document.createElement('button');
          gpu.type = 'button';
          gpu.className = 'gpu-download model-download picker-gpu';
          gpu.dataset.gpu = '1';
          gpu.textContent = '下载 GPU 环境';
          row.append(gpu);
          const ready = document.createElement('span');
          ready.className = 'model-ready is-ready gpu-ready';
          ready.dataset.gpuReady = '1'; ready.textContent = '已就绪'; ready.hidden = true;
          row.append(ready);
        }
        return row;
      }));
      sync(); menu.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      position();
      if (focusOption) (menu.querySelector('[aria-selected="true"]:not(:disabled)')
        || menu.querySelector('[data-value]:not(:disabled)'))?.focus();
    }
    button.addEventListener('click', () => menu.hidden ? open() : close());
    menu.addEventListener('click', event => {
      const gpuAction = event.target.closest('[data-gpu]');
      if (gpuAction) {
        if (!gpuAction.disabled) document.querySelector('#gpuDownload')?.click();
        return;
      }
      const item = event.target.closest('[data-value]');
      if (!item || item.disabled || select.disabled) return;
      if (select.value !== item.dataset.value) {
        select.value = item.dataset.value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
      }
      sync(); close(true);
    });
    picker.addEventListener('keydown', event => {
      const item = event.target.closest('[data-value]');
      if (item && ['Enter', ' '].includes(event.key)) {
        event.preventDefault();
        if (!event.repeat && !item.disabled) item.click();
        return;
      }
      if (event.key === 'Escape') { event.preventDefault(); close(true); return; }
      if (event.key === 'Tab') { close(); return; }
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) || select.disabled) return;
      event.preventDefault();
      if (menu.hidden) { open(true); return; }
      const options = Array.from(menu.querySelectorAll('[data-value]:not(:disabled)'));
      if (!options.length) return;
      const index = options.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
      options[next].focus();
    });
    picker.addEventListener('focusout', event => {
      if (!picker.contains(event.relatedTarget)) close();
    });
    document.addEventListener('pointerdown', event => {
      if (!picker.contains(event.target)) close();
    });
    window.addEventListener('resize', () => close());
    window.addEventListener('scroll', event => {
      if (!menu.contains(event.target)) position();
    }, true);
    select.addEventListener('change', sync);
    new MutationObserver(sync).observe(select, { attributes: true, childList: true, subtree: true });
    const gpuSource = document.querySelector('#gpuDownload');
    if (gpuSource) {
      new MutationObserver(syncGpu).observe(gpuSource, {
        attributes: true, childList: true, characterData: true, subtree: true,
      });
    }
    const gpuBox = document.querySelector('#gpuState');
    if (gpuBox) {
      new MutationObserver(() => { syncGpu(); sync(); }).observe(gpuBox, { attributes: true, subtree: true });
    }
    sync();
    return { sync, close };
  }

  const pickers = [
    attach(document.querySelector('#deviceSelect'), 'device-picker'),
    attach(document.querySelector('#filterSelect'), 'filter-picker'),
    attach(document.querySelector('#exportFormat'), 'export-format-picker'),
  ];
  window.SubtitleDevicePicker = { sync: () => pickers[0]?.sync() };
  window.SubtitlePickers = { syncAll: () => pickers.forEach(picker => picker?.sync()) };
})();
