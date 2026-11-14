/* SelfHosted Agent Kit - vanilla front-end. No build step, no framework. */
(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const state = {
    token: localStorage.getItem('agent_token') || '',
    sessionId: null,
    controller: null,
    config: null,
  };

  /* ------------------------------------------------------------ utilities */
  const escapeHtml = (text) =>
    String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /** Tiny Markdown renderer: fenced code, tables, bold, inline code, links. */
  function renderMarkdown(raw) {
    const blocks = [];
    let text = String(raw).replace(/```([a-z0-9]*)\n([\s\S]*?)```/gi, (_, lang, code) => {
      blocks.push(`<pre><code data-lang="${escapeHtml(lang)}">${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`);
      return `\u0000BLOCK${blocks.length - 1}\u0000`;
    });
    text = escapeHtml(text);
    text = text.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    text = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    text = renderTables(text);
    return text.replace(/\u0000BLOCK(\d+)\u0000/g, (_, i) => blocks[Number(i)]);
  }

  function renderTables(text) {
    return text.replace(/(^\|.*\|\s*$\n)(^\|[\s:|-]+\|\s*$\n)((?:^\|.*\|\s*$\n?)+)/gm, (_, head, _sep, body) => {
      const cells = (row) => row.trim().replace(/^\||\|$/g, '').split('|').map((c) => `<td>${c.trim()}</td>`).join('');
      const header = head.trim().replace(/^\||\|$/g, '').split('|').map((c) => `<th>${c.trim()}</th>`).join('');
      const rows = body.trim().split('\n').map((row) => `<tr>${cells(row)}</tr>`).join('');
      return `<table><thead><tr>${header}</tr></thead><tbody>${rows}</tbody></table>`;
    });
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${state.token}`,
        ...(options.headers || {}),
      },
    });
    if (response.status === 401) {
      showAuth('Invalid or expired token.');
      throw new Error('unauthorized');
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  /* ------------------------------------------------------------------ auth */
  function showAuth(message) {
    $('#auth').hidden = false;
    $('#app').hidden = true;
    $('#auth-error').hidden = !message;
    if (message) $('#auth-error').textContent = message;
  }

  function showApp() {
    $('#auth').hidden = true;
    $('#app').hidden = false;
  }

  $('#auth-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    state.token = $('#token-input').value.trim();
    if (!state.token) return;
    try {
      state.config = await api('/api/config');
      localStorage.setItem('agent_token', state.token);
      showApp();
      boot();
    } catch (error) {
      if (error.message !== 'unauthorized') showAuth(error.message);
    }
  });

  $('#disconnect').addEventListener('click', () => {
    localStorage.removeItem('agent_token');
    state.token = '';
    state.sessionId = null;
    $('#messages').innerHTML = '';
    showAuth('');
  });

  /* ------------------------------------------------------------------ boot */
  async function boot() {
    $('#model-badge').textContent = state.config ? state.config.model : '';
    await loadSessions();
  }

  async function loadSessions() {
    try {
      const { sessions } = await api('/api/sessions');
      const nav = $('#sessions');
      nav.innerHTML = '';
      sessions.forEach((session) => {
        const button = document.createElement('button');
        button.className = 'session-item' + (session.id === state.sessionId ? ' active' : '');
        button.textContent = session.title || 'Untitled';
        button.title = `${session.messages} messages - ${session.updated}`;
        button.addEventListener('click', () => openSession(session.id));
        nav.appendChild(button);
      });
    } catch (_) { /* offline: keep the UI usable */ }
  }

  /* -------------------------------------------------------------- messages */
  function addUserMessage(text) {
    const div = document.createElement('div');
    div.className = 'msg msg-user';
    div.textContent = text;
    $('#messages').appendChild(div);
    scrollDown();
  }

  function newAssistantBubble() {
    const wrapper = document.createElement('div');
    wrapper.className = 'msg msg-assistant';
    const reasoning = document.createElement('div');
    reasoning.className = 'reasoning';
    reasoning.hidden = true;
    const text = document.createElement('div');
    text.className = 'msg-text';
    // Notices live in their own node: appending them to `text` would be wiped
    // by the next streamed chunk.
    const notices = document.createElement('div');
    notices.className = 'notices';
    wrapper.append(reasoning, text, notices);
    $('#messages').appendChild(wrapper);
    return { wrapper, reasoning, text, notices, tools: new Map(), buffer: '' };
  }

  function toolCard(bubble, event, finished) {
    let card = bubble.tools.get(event.id);
    if (!card) {
      card = document.createElement('details');
      card.className = 'tool';
      card.innerHTML = `
        <summary>
          <span class="tool-name"></span>
          <span class="tool-args"></span>
          <span class="badge tool-status">running</span>
        </summary>
        <pre></pre>`;
      card.querySelector('.tool-name').textContent = event.name || 'tool';
      const args = (event.args || '').replace(/\s+/g, ' ');
      card.querySelector('.tool-args').textContent = args.length > 90 ? args.slice(0, 90) + '\u2026' : args;
      card.dataset.args = event.args || '';
      bubble.wrapper.insertBefore(card, bubble.text);
      bubble.tools.set(event.id, card);
    }
    if (finished) {
      const status = card.querySelector('.tool-status');
      status.textContent = event.is_error ? 'error' : 'done';
      if (event.is_error) card.classList.add('is-error');
      card.querySelector('pre').textContent = event.output || '';
    }
    return card;
  }

  function approvalButtons(card, id) {
    card.open = true;
    const box = document.createElement('div');
    box.className = 'approval';
    box.innerHTML = '<b>Allow this action?</b><button class="primary" data-ok="1">Allow</button><button data-ok="0" class="danger">Refuse</button>';
    const pre = card.querySelector('pre');
    try { pre.textContent = JSON.stringify(JSON.parse(card.dataset.args || '{}'), null, 2); } catch (e) {}
    card.querySelector('.tool-status').textContent = 'waiting';
    box.onclick = async (e) => {
      const ok = e.target.dataset.ok;
      if (ok === undefined) return;
      box.textContent = '\u2026';
      try { await api('/api/approve', { method: 'POST', body: JSON.stringify({ id, ok: ok === '1' }) }); } catch (err) {}
      box.remove();
    };
    card.appendChild(box);
  }

  function scrollDown() {
    const box = $('#messages');
    box.scrollTop = box.scrollHeight;
  }

  /* ----------------------------------------------------------------- chat */
  async function send(message) {
    addUserMessage(message);
    const bubble = newAssistantBubble();
    const input = $('#input');
    input.value = '';
    input.style.height = 'auto';
    setBusy(true);
    state.controller = new AbortController();

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        signal: state.controller.signal,
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.token}` },
        body: JSON.stringify({ session_id: state.sessionId, message }),
      });
      if (response.status === 401) { showAuth('Invalid or expired token.'); return; }
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = '';

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        pending += decoder.decode(value, { stream: true });
        const chunks = pending.split('\n\n');
        pending = chunks.pop() || '';
        for (const chunk of chunks) {
          const line = chunk.split('\n').find((l) => l.startsWith('data:'));
          if (!line) continue;
          const raw = line.slice(5).trim();
          if (!raw || raw === '[DONE]') continue;
          let event;
          try { event = JSON.parse(raw); } catch (_) { continue; }
          handleEvent(event, bubble);
        }
      }
    } catch (error) {
      if (error.name !== 'AbortError') {
        bubble.text.innerHTML += `<p class="error">${escapeHtml(error.message)}</p>`;
      }
    } finally {
      setBusy(false);
      state.controller = null;
      await loadSessions();
    }
  }

  function handleEvent(event, bubble) {
    switch (event.type) {
      case 'text':
        bubble.buffer += event.delta;
        bubble.text.innerHTML = renderMarkdown(bubble.buffer);
        scrollDown();
        break;
      case 'reasoning':
        bubble.reasoning.hidden = false;
        bubble.reasoning.textContent += event.delta;
        break;
      case 'step':
        $('#step-badge').hidden = false;
        $('#step-badge').textContent = `step ${event.step}/${event.max}`;
        break;
      case 'tool_call':
        toolCard(bubble, event, false);
        scrollDown();
        break;
      case 'approval':
        approvalButtons(toolCard(bubble, event, false), event.id);
        scrollDown();
        break;
      case 'tool_result':
        toolCard(bubble, event, true);
        break;
      case 'error':
        bubble.notices.insertAdjacentHTML(
          'beforeend',
          `<p class="error">${escapeHtml(event.message)}</p>`
        );
        break;
      case 'warning':
        bubble.notices.insertAdjacentHTML(
          'beforeend',
          `<p class="muted">${escapeHtml(event.message)}</p>`
        );
        break;
      case 'final':
        if (event.session_id && event.session_id !== state.sessionId) {
          state.sessionId = event.session_id;
        }
        if (!bubble.buffer && event.text) {
          bubble.buffer = event.text;
          bubble.text.innerHTML = renderMarkdown(event.text);
        }
        $('#step-badge').hidden = true;
        break;
      default:
        break;
    }
  }

  function setBusy(busy) {
    $('#send').disabled = busy;
    $('#stop').hidden = !busy;
    $('#input').disabled = busy;
  }

  /* -------------------------------------------------------------- sessions */
  async function openSession(id) {
    try {
      const session = await api(`/api/sessions/${id}`);
      state.sessionId = session.id;
      $('#chat-title').textContent = session.title || 'Conversation';
      const box = $('#messages');
      box.innerHTML = '';
      (session.messages || []).forEach((message) => {
        if (message.role === 'user') addUserMessage(message.content || '');
        else if (message.role === 'assistant' && (message.content || '').trim()) {
          const div = document.createElement('div');
          div.className = 'msg msg-assistant';
          const inner = document.createElement('div');
          inner.className = 'msg-text';
          inner.innerHTML = renderMarkdown(message.content);
          div.appendChild(inner);
          box.appendChild(div);
        }
      });
      scrollDown();
      loadSessions();
    } catch (error) {
      console.error(error);
    }
  }

  $('#new-chat').addEventListener('click', () => {
    state.sessionId = null;
    $('#messages').innerHTML = '';
    $('#chat-title').textContent = 'New conversation';
    loadSessions();
    $('#input').focus();
  });

  /* -------------------------------------------------------------- composer */
  $('#composer').addEventListener('submit', (event) => {
    event.preventDefault();
    const value = $('#input').value.trim();
    if (value) send(value);
  });

  $('#stop').addEventListener('click', () => {
    if (state.controller) state.controller.abort();
  });

  const input = $('#input');
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      $('#composer').requestSubmit();
    }
  });

  /* --------------------------------------------------------------- dialogs */
  $('#open-memory').addEventListener('click', async () => {
    const dialog = $('#memory-dialog');
    const list = $('#memory-list');
    list.innerHTML = '<li class="muted">loading...</li>';
    dialog.showModal();
    try {
      const { memories } = await api('/api/memory');
      list.innerHTML = memories.length
        ? memories.map((m) => `<li><span class="memory-name">${escapeHtml(m.name)}</span> <span class="muted">(${escapeHtml(m.type)})</span><br>${escapeHtml(m.description)}</li>`).join('')
        : '<li class="muted">No memories yet.</li>';
    } catch (error) {
      list.innerHTML = `<li class="error">${escapeHtml(error.message)}</li>`;
    }
  });

  $('#toggle-config').addEventListener('click', async () => {
    try {
      state.config = await api('/api/config');
      $('#config-json').textContent = JSON.stringify(state.config, null, 2);
      $('#config-dialog').showModal();
    } catch (_) { /* handled by api() */ }
  });

  /* ---------------------------------------------------------------- start */
  if (state.token) {
    api('/api/config')
      .then((config) => {
        state.config = config;
        showApp();
        boot();
      })
      .catch(() => showAuth(''));
  } else {
    showAuth('');
  }
})();
