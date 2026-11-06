/**
 * J.A.R.V.I.S. control panel.
 * All server data is written with textContent (through h()), never innerHTML.
 */
const API = '/api/jarvis';
let _open = false;
let _tab = 'painel';
let _renderToken = 0;

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'class') node.className = value;
    else if (key === 'style') node.setAttribute('style', value);
    else if (key === 'value') node.value = value;
    else if (key === 'checked') node.checked = !!value;
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : String(child));
  }
  return node;
}

class ApiError extends Error {
  constructor(status, body) {
    super(body?.detail ? String(body.detail) : `Erro ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function api(path, options = {}) {
  const init = { credentials: 'same-origin', ...options };
  if (options.json !== undefined) {
    init.method = options.method || 'POST';
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(options.json);
    delete init.json;
  }
  const res = await fetch(API + path, init);
  let body = null;
  try { body = await res.json(); } catch { /* empty response */ }
  if (!res.ok) throw new ApiError(res.status, body);
  return body;
}

const fmtBytes = (n) => {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
};
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' }) : '—');

const card = (title, ...children) => h('div', { class: 'admin-card jarvis-card' }, title ? h('h2', {}, title) : null, ...children);
const sub = (text) => h('div', { class: 'admin-toggle-sub' }, text);
const btn = (label, onclick, kind = 'add', attrs = {}) => h('button', { type: 'button', class: kind === 'delete' ? 'admin-btn-delete jarvis-btn' : 'admin-btn-add jarvis-btn', onclick, ...attrs }, label);
const input = (attrs = {}) => h('input', { class: 'memory-search-input jarvis-input', type: 'text', ...attrs });
const stat = (label, value, tone) => h('div', { class: 'jarvis-stat' }, h('div', { class: `jarvis-stat-value ${tone ? `jarvis-${tone}` : ''}` }, value), h('div', { class: 'admin-toggle-sub' }, label));
const badge = (text, tone) => h('span', { class: `jarvis-badge ${tone ? `jarvis-${tone}` : ''}` }, text);
const row = (...children) => h('div', { class: 'jarvis-row' }, ...children);
const empty = (text) => h('div', { class: 'admin-toggle-sub jarvis-empty' }, text);

function feedback(text, tone = 'ok') {
  const area = document.getElementById('jarvis-feedback');
  if (!area) return;
  area.textContent = text;
  area.className = `jarvis-feedback jarvis-${tone}`;
  clearTimeout(feedback._t);
  feedback._t = setTimeout(() => { area.textContent = ''; area.className = 'jarvis-feedback'; }, 6000);
}

/** Run an action, reporting pending approvals and errors in a uniform way. */
async function act(fn, success) {
  try {
    const result = await fn();
    if (result?.pending_approval) feedback('Aguardando sua aprovação na aba Aprovações.', 'warn');
    else if (success) feedback(typeof success === 'function' ? success(result) : success);
    return result;
  } catch (err) {
    if (err.body?.reconnect_required) feedback('A conta Google precisa ser reconectada em Integrações.', 'err');
    else if (err.body?.missing_scope) feedback(`Autorize ${err.body.missing_scope} em Integrações.`, 'err');
    else feedback(err.message || 'Falha na operação.', 'err');
    return null;
  }
}

const statusTone = { connected: 'ok', sent: 'ok', completed: 'ok', succeeded: 'ok', fact: 'ok', executed: 'ok', reauth_required: 'err', failed: 'err', conflicted: 'err', rejected: 'err', uncertain: 'warn', awaiting_approval: 'warn', waiting_approval: 'warn', hypothesis: 'warn', pending: 'warn', running: 'warn' };
const STATUS_PT = { connected: 'conectada', reauth_required: 'reconectar', fact: 'fato', hypothesis: 'hipótese', conflicted: 'em conflito', pending: 'pendente', running: 'executando', succeeded: 'concluído', failed: 'falhou', cancelled: 'cancelado', active: 'ativa', completed: 'concluída', waiting_approval: 'aguardando aprovação', awaiting_approval: 'aguardando aprovação', queued: 'na fila', sending: 'enviando', sent: 'enviado', uncertain: 'verificando', approved: 'aprovado', rejected: 'rejeitado', executed: 'executado', expired: 'expirado' };
const statusBadge = (s) => badge(STATUS_PT[s] || s, statusTone[s]);

const KIND_PT = { semantic: 'semântica', episodic: 'episódica', procedural: 'procedural', operational: 'operacional' };
const CATEGORY_PT = { fact: 'fato', preference: 'preferência', identity: 'identidade', foto: 'foto', missao: 'missão' };
const SOURCE_PT = { user: 'você', assistant: 'assistente', document: 'documento', email: 'e-mail', web: 'internet', system: 'sistema', import: 'importada', tool: 'ferramenta', attachment: 'anexo' };

function jarvisMark() {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'jarvis-mark');
  svg.setAttribute('aria-hidden', 'true');
  for (const [r, filled] of [[10, false], [4, true]]) {
    const circle = document.createElementNS(NS, 'circle');
    circle.setAttribute('cx', '12');
    circle.setAttribute('cy', '12');
    circle.setAttribute('r', String(r));
    circle.setAttribute('fill', filled ? 'currentColor' : 'none');
    circle.setAttribute('stroke', 'currentColor');
    circle.setAttribute('stroke-width', '2');
    svg.append(circle);
  }
  return svg;
}

// ---- Painel ----
async function renderPainel(root) {
  const [status, candidates, notes] = await Promise.all([api('/status'), api('/learning/candidates'), api('/notifications')]);
  let monitor = null;
  try { monitor = await api('/monitor'); } catch { /* non-admins don't see system data */ }
  const brief = h('div', { class: 'jarvis-answer', 'aria-live': 'polite' });
  const answer = await api('/command', { json: { text: 'Jarvis, bom dia' } }).catch(() => null);
  if (answer?.text) brief.textContent = answer.text;
  root.append(
    card(null, h('div', { class: 'jarvis-identity' }, jarvisMark(), h('div', {}, h('div', { class: 'jarvis-title' }, status.identity.name), sub(status.identity.meaning))), brief),
    h('div', { class: 'jarvis-stats' },
      stat('Memórias', status.memory.total),
      stat('Aprovações pendentes', status.approvals_pending, status.approvals_pending ? 'warn' : null),
      stat('Para revisar', status.learning_pending, status.learning_pending ? 'warn' : null),
      stat('Não lidas', status.notifications_unread),
      monitor && stat('Modelos', monitor.models.total),
      monitor && stat('Custo no mês', `US$ ${monitor.usage.month_cost_usd.toFixed(2)}`, monitor.usage.budget_exceeded ? 'err' : null)),
    monitor && card('Serviços', h('div', { class: 'jarvis-chips' }, monitor.services.map((s) => badge(`${s.name}: ${s.online ? 'online' : 'offline'}`, s.online ? 'ok' : (s.required ? 'err' : null))))),
    candidates.candidates.length ? card('Revisar antes de guardar', candidates.candidates.slice(0, 3).map(candidateRow), sub('Veja todos na aba Memória.')) : null,
    notes.notifications.length ? card('Notificações',
      notes.notifications.slice(0, 8).map((n) => h('div', { class: 'jarvis-turn' }, h('strong', {}, n.title), n.read ? null : ' ', n.read ? null : badge('nova', 'warn'), n.body ? h('div', {}, n.body) : null, sub(fmtDate(n.created_at)))),
      status.notifications_unread ? btn('Marcar como lidas', async () => { if (await act(() => api('/notifications/read', { method: 'POST' }), 'Notificações lidas.')) render(); }) : null) : null,
  );
}

function candidateRow(c) {
  const line = row(h('div', { class: 'grow' }, c.text, h('div', {}, badge(CATEGORY_PT[c.category] || c.category), ' ', badge(c.source_type === 'user' ? 'você disse' : `origem: ${SOURCE_PT[c.source_type] || c.source_type}`, c.source_type === 'user' ? null : 'warn'))),
    btn('Guardar', async () => { if (await act(() => api(`/learning/candidates/${c.id}`, { json: { approve: true } }), 'Guardado.')) line.remove(); }),
    btn('Descartar', async () => { if (await act(() => api(`/learning/candidates/${c.id}`, { json: { approve: false } }))) line.remove(); }, 'delete'));
  return line;
}

// ---- Voz / comando ----
const voiceState = { stream: null, recorder: null, chunks: [], audio: null, handsFree: false, vadTimer: null, ctx: null };

function stopSpeaking() {
  if (voiceState.audio) { voiceState.audio.pause(); voiceState.audio = null; }
  window.speechSynthesis?.cancel();
}

function speak(reply, audioB64) {
  stopSpeaking();
  if (audioB64) {
    const bytes = Uint8Array.from(atob(audioB64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: 'audio/mpeg' }));
    voiceState.audio = new Audio(url);
    voiceState.audio.addEventListener('ended', () => URL.revokeObjectURL(url));
    voiceState.audio.volume = Number(document.getElementById('jarvis-volume')?.value ?? 1);
    voiceState.audio.play().catch(() => {});
  } else if (window.speechSynthesis && reply) {
    const utterance = new SpeechSynthesisUtterance(reply);
    utterance.lang = 'pt-BR';
    utterance.volume = Number(document.getElementById('jarvis-volume')?.value ?? 1);
    window.speechSynthesis.speak(utterance);
  }
}

function setMic(active, label) {
  const el = document.getElementById('jarvis-mic-state');
  if (!el) return;
  el.textContent = label;
  el.className = `jarvis-badge ${active ? 'jarvis-err' : ''}`;
}

async function sendTurn(form, log) {
  setMic(false, 'processando');
  const res = await act(() => api('/voice/turn', { method: 'POST', body: form }));
  setMic(!!voiceState.stream, voiceState.stream ? 'microfone ativo' : 'microfone desligado');
  if (!res || res.handled_by === 'no_wake_word' || res.handled_by === 'silence') return;
  log.prepend(h('div', { class: 'jarvis-turn' }, h('div', { class: 'admin-toggle-sub' }, res.transcript), h('div', {}, res.reply)));
  if (document.getElementById('jarvis-speak')?.checked) speak(res.reply, res.audio);
}

async function openMic() {
  voiceState.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  return voiceState.stream;
}

function closeMic() {
  if (voiceState.browserHands) {
    const recognizer = voiceState.browserHands;
    voiceState.browserHands = null;
    recognizer.stop();
  }
  clearInterval(voiceState.vadTimer);
  voiceState.ctx?.close().catch(() => {});
  voiceState.stream?.getTracks().forEach((t) => t.stop());
  Object.assign(voiceState, { stream: null, recorder: null, handsFree: false, vadTimer: null, ctx: null });
  setMic(false, 'microfone desligado');
}

function record(stream, onDone) {
  const recorder = new MediaRecorder(stream);
  const chunks = [];
  recorder.ondataavailable = (e) => chunks.push(e.data);
  recorder.onstop = () => onDone(new Blob(chunks, { type: recorder.mimeType }));
  recorder.start();
  return recorder;
}

function encodeWav(chunks, sampleRate) {
  const length = chunks.reduce((sum, c) => sum + c.length, 0);
  const view = new DataView(new ArrayBuffer(44 + length * 2));
  const text = (offset, value) => { for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i)); };
  text(0, 'RIFF'); view.setUint32(4, 36 + length * 2, true); text(8, 'WAVEfmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, 'data'); view.setUint32(40, length * 2, true);
  let offset = 44;
  for (const chunk of chunks) {
    for (let i = 0; i < chunk.length; i += 1) {
      view.setInt16(offset, Math.max(-1, Math.min(1, chunk[i])) * 0x7fff, true);
      offset += 2;
    }
  }
  return new Blob([view], { type: 'audio/wav' });
}

/**
 * Hands-free: voice detection runs locally, on raw samples, keeping about a second
 * of audio from before speech starts. Without it the start of a phrase, which is
 * exactly the wake word, would be lost. Audio leaves only when speech was detected.
 */
async function startHandsFree(log) {
  const stream = await openMic();
  voiceState.handsFree = true;
  const ctx = new AudioContext();
  voiceState.ctx = ctx;
  await ctx.resume();
  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(4096, 1, 1);
  const silent = ctx.createGain();
  silent.gain.value = 0;
  source.connect(processor);
  processor.connect(silent);
  silent.connect(ctx.destination);
  const blockSeconds = 4096 / ctx.sampleRate;
  const preRollBlocks = Math.ceil(1 / blockSeconds);
  const silenceBlocks = Math.ceil(1.2 / blockSeconds);
  const maxBlocks = Math.ceil(30 / blockSeconds);
  const preRoll = [];
  let speech = null;
  let quiet = 0;
  setMic(true, 'ouvindo: diga "Jarvis"');
  processor.onaudioprocess = (event) => {
    const block = new Float32Array(event.inputBuffer.getChannelData(0));
    const energy = Math.sqrt(block.reduce((sum, v) => sum + v * v, 0) / block.length);
    if (!speech) {
      if (energy > 0.02) {
        stopSpeaking(); // barge-in: talking interrupts the assistant
        speech = [...preRoll, block];
        preRoll.length = 0;
        quiet = 0;
      } else {
        preRoll.push(block);
        if (preRoll.length > preRollBlocks) preRoll.shift();
      }
      return;
    }
    speech.push(block);
    quiet = energy > 0.02 ? 0 : quiet + 1;
    if (quiet >= silenceBlocks || speech.length >= maxBlocks) {
      const form = new FormData();
      form.append('audio', encodeWav(speech, ctx.sampleRate), 'fala.wav');
      form.append('wake_word', 'true');
      speech = null;
      sendTurn(form, log);
    }
  };
}

function browserRecognizer(onText, { continuous = false } = {}) {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) return null;
  const recognizer = new Recognition();
  recognizer.lang = 'pt-BR';
  recognizer.continuous = continuous;
  recognizer.interimResults = false;
  recognizer.onresult = (event) => {
    const result = event.results[event.results.length - 1];
    if (result?.isFinal) onText(result[0].transcript);
  };
  return recognizer;
}

async function renderVoz(root) {
  const status = await api('/voice/status');
  const hasBrowserSpeech = !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  const history = await api('/voice/history');
  const log = h('div', { class: 'jarvis-log' }, history.turns.map((t) => h('div', { class: 'jarvis-turn' }, h('div', { class: 'admin-toggle-sub' }, t.transcript), h('div', {}, t.reply))));
  const text = input({ placeholder: 'Ex.: Jarvis, quais compromissos tenho hoje?', 'aria-label': 'Comando', onkeydown: (e) => { if (e.key === 'Enter') sendText(); } });
  const sendText = async () => {
    if (!text.value.trim()) return;
    const form = new FormData();
    form.append('text', text.value);
    text.value = '';
    await sendTurn(form, log);
  };
  const talk = btn('Segurar para falar', null);
  let recorder = null;
  let browserTalk = null;
  const sendSpoken = (spoken, wake) => {
    const form = new FormData();
    form.append('text', spoken);
    if (wake) form.append('wake_word', 'true');
    sendTurn(form, log);
  };
  const pressStart = async () => {
    stopSpeaking();
    if (!status.stt) {
      browserTalk = browserRecognizer((spoken) => sendSpoken(spoken, false));
      if (!browserTalk) { feedback('Este navegador não reconhece voz; configure o STT no servidor.', 'warn'); return; }
      setMic(true, 'ouvindo');
      browserTalk.onend = () => setMic(false, 'microfone desligado');
      browserTalk.start();
      return;
    }
    const stream = await act(openMic);
    if (!stream) return;
    setMic(true, 'gravando');
    recorder = record(stream, (blob) => {
      closeMic();
      const form = new FormData();
      form.append('audio', blob, 'fala.webm');
      sendTurn(form, log);
    });
  };
  const pressEnd = () => {
    browserTalk?.stop();
    browserTalk = null;
    if (recorder?.state === 'recording') recorder.stop();
    recorder = null;
  };
  talk.addEventListener('pointerdown', pressStart);
  talk.addEventListener('pointerup', pressEnd);
  talk.addEventListener('pointerleave', pressEnd);
  const hands = h('input', { type: 'checkbox', 'aria-label': 'Mãos livres', onchange: async (e) => {
    if (!e.target.checked) {
      voiceState.browserHands?.stop();
      voiceState.browserHands = null;
      closeMic();
      return;
    }
    if (status.stt) { await act(() => startHandsFree(log)); return; }
    const recognizer = browserRecognizer((spoken) => sendSpoken(spoken, true), { continuous: true });
    if (!recognizer) { e.target.checked = false; feedback('Este navegador não reconhece voz; configure o STT no servidor.', 'warn'); return; }
    voiceState.browserHands = recognizer;
    recognizer.onend = () => { if (voiceState.browserHands === recognizer) recognizer.start(); };
    setMic(true, 'ouvindo: diga "Jarvis"');
    recognizer.start();
  } });
  root.append(
    card('Comando', row(text, btn('Enviar', sendText)),
      sub('Comandos de agenda, memória e dispositivos são respondidos sem gastar tokens.')),
    card('Voz', h('div', { class: 'jarvis-chips' }, badge(`Transcrição: ${status.stt ? 'no servidor' : hasBrowserSpeech ? 'do navegador' : 'não configurada'}`, status.stt ? 'ok' : 'warn'), badge(`Fala: ${status.tts ? 'servidor' : 'navegador'}`, status.tts ? 'ok' : null), h('span', { id: 'jarvis-mic-state', class: 'jarvis-badge' }, 'microfone desligado')),
      row(talk, btn('Parar fala', stopSpeaking, 'delete')),
      row(h('label', { class: 'jarvis-check' }, hands, ' Mãos livres (só com a aba aberta)'), h('label', { class: 'jarvis-check' }, h('input', { type: 'checkbox', id: 'jarvis-speak', checked: true }), ' Falar respostas'), h('label', { class: 'jarvis-check' }, 'Volume ', h('input', { type: 'range', id: 'jarvis-volume', min: 0, max: 1, step: 0.1, value: 1 }))),
      sub(status.stt
        ? `O áudio é transcrito no seu servidor e não é gravado em disco${status.store_audio ? ' (armazenamento ligado)' : ''}. No modo mãos livres, ele só é enviado quando há fala, e o Jarvis só age se ouvir o nome dele.`
        : 'Sem transcrição no servidor, é usado o reconhecimento do navegador. No Chrome, isso envia o áudio ao Google enquanto o microfone estiver ativo.')),
    card('Histórico', log, btn('Apagar histórico', async () => { if (await act(() => api('/voice/history', { method: 'DELETE' }), 'Histórico apagado.')) log.replaceChildren(); }, 'delete')),
  );
}

// ---- Modelos ----
async function renderModelos(root) {
  const data = await api('/models');
  const usage = await api('/usage');
  const select = h('select', { class: 'jarvis-input', 'aria-label': 'Perfil', onchange: async (e) => { if (await act(() => api('/config', { method: 'PATCH', json: { router: { profile: e.target.value } } }), 'Perfil atualizado.')) render(); } },
    Object.entries(data.profiles).map(([k, label]) => h('option', { value: k, selected: k === data.profile }, label)));
  const mode = h('select', { class: 'jarvis-input', 'aria-label': 'Modelos pagos', onchange: (e) => act(() => api('/config', { method: 'PATCH', json: { router: { paid_fallback_mode: e.target.value } } }), 'Regra de custo atualizada.') },
    [['never', 'Nunca usar pagos como fallback'], ['configured', 'Só os que listei na cadeia de fallback'], ['always', 'O roteador pode escolher qualquer um']].map(([v, l]) => h('option', { value: v, selected: v === data.paid_fallback_mode }, l)));
  const tierBadge = (t) => badge({ local: 'local', free: 'grátis', paid: 'pago' }[t] || t, t === 'paid' ? 'warn' : 'ok');
  root.append(
    card('Roteamento', row(h('label', { class: 'grow' }, 'Perfil ', select), h('label', { class: 'grow' }, 'Pagos ', mode)), sub(`${data.catalog_total} ${data.catalog_total === 1 ? 'modelo' : 'modelos'} no catálogo. A seleção manual no chat continua valendo.`)),
    card('Uso (30 dias)', h('div', { class: 'jarvis-stats' }, stat('Custo no mês', `US$ ${usage.month_cost_usd.toFixed(4)}`, usage.budget_exceeded ? 'err' : null), stat('Orçamento', usage.monthly_budget_usd ? `US$ ${usage.monthly_budget_usd}` : 'sem limite'), stat('Chamadas sem preço', usage.calls_without_price, usage.calls_without_price ? 'warn' : null)),
      usage.calls_without_price ? sub('Sem preço conhecido não entram no custo. Atualize os preços do OpenRouter abaixo ou configure-os.') : null,
      h('div', { class: 'jarvis-table' }, usage.models.slice(0, 15).map((m) => row(h('div', { class: 'grow jarvis-mono' }, m.model), badge(`${m.calls}x`), badge(`${m.input_tokens + m.output_tokens} tok`), m.errors ? badge(`${m.errors} erros`, 'err') : null, tierBadge(m.tier)))),
      btn('Atualizar preços do OpenRouter', () => act(() => api('/pricing/refresh', { method: 'POST' }), (r) => `${r.updated} preços atualizados.`))),
    card('Ranking do perfil', h('div', { class: 'jarvis-table' }, data.models.slice(0, 30).map((m) => row(h('div', { class: 'grow' }, h('div', { class: 'jarvis-mono' }, m.model), m.reason ? sub(m.reason) : null), m.avg_latency_ms ? badge(`${m.avg_latency_ms} ms`) : null, tierBadge(m.tier), badge(m.provider))))),
  );
}

// ---- Memória ----
async function renderMemoria(root) {
  const list = h('div', { class: 'jarvis-table' });
  const query = input({ placeholder: 'Buscar por significado ou palavra', 'aria-label': 'Buscar', onkeydown: (e) => { if (e.key === 'Enter') load(); } });
  const kind = h('select', { class: 'jarvis-input', 'aria-label': 'Tipo', onchange: () => load() }, [['', 'Todos os tipos'], ['semantic', 'Semântica'], ['episodic', 'Episódica'], ['procedural', 'Procedural'], ['operational', 'Operacional']].map(([v, l]) => h('option', { value: v }, l)));
  const hypo = h('input', { type: 'checkbox', onchange: () => load() });
  const statsBox = h('div', { class: 'jarvis-stats' });
  const item = (m) => {
    const line = row(h('div', { class: 'grow' }, m.text, h('div', {}, statusBadge(m.status), ' ', badge(KIND_PT[m.kind] || m.kind), ' ', badge(`origem: ${SOURCE_PT[m.source_type] || m.source_type}`), m.version > 1 ? badge(`v${m.version}`) : null)),
      btn('Corrigir', async () => {
        const area = h('textarea', { class: 'jarvis-input', rows: 2 }, m.text);
        line.replaceChildren(area, btn('Salvar', async () => { if (await act(() => api(`/memory/${m.id}`, { method: 'PATCH', json: { text: area.value } }), 'Corrigido; a versão anterior ficou no histórico.')) load(); }), btn('Voltar', () => load(), 'delete'));
      }),
      btn('Excluir', async () => { if (await act(() => api(`/memory/${m.id}`, { method: 'DELETE' }), 'Excluída.')) line.remove(); }, 'delete'));
    return line;
  };
  const load = async () => {
    const params = new URLSearchParams({ limit: 100 });
    if (query.value.trim()) params.set('q', query.value.trim());
    if (kind.value) params.set('kind', kind.value);
    if (hypo.checked) params.set('include_hypotheses', 'true');
    const data = await act(() => api(`/memory?${params}`));
    if (!data) return;
    list.replaceChildren(...(data.items.length ? data.items.map(item) : [empty('Nada encontrado.')]));
    statsBox.replaceChildren(stat('Total', data.stats.total), ...Object.entries(data.stats.by_kind).map(([k, n]) => stat(KIND_PT[k] || k, n)), data.stats.by_status.conflicted ? stat('Em conflito', data.stats.by_status.conflicted, 'err') : '');
  };
  const newText = input({ placeholder: 'O que devo lembrar?', 'aria-label': 'Nova memória' });
  const [candidates, conflictData] = await Promise.all([api('/learning/candidates'), api('/memory/conflicts')]);
  const conflictCard = (pair) => {
    const box = h('div', { class: 'jarvis-mission' });
    const keep = (winner, loser) => btn('Esta está certa', async () => {
      if (await act(() => api('/memory/resolve', { json: { keep_id: winner.id, drop_id: loser.id } }), 'Conflito resolvido.')) { box.remove(); load(); }
    });
    box.append(row(h('div', { class: 'grow' }, pair.a.text), keep(pair.a, pair.b)), row(h('div', { class: 'grow' }, pair.b.text), keep(pair.b, pair.a)));
    return box;
  };
  root.append(
    statsBox,
    conflictData.conflicts.length ? card('Conflitos para decidir', conflictData.conflicts.map(conflictCard), sub('A outra versão sai da busca, mas continua no histórico.')) : null,
    card('Nova memória', row(newText, btn('Guardar', async () => {
      const out = await act(() => api('/memory', { json: { text: newText.value } }), (r) => (r.action === 'conflict' ? 'Guardada, mas conflita com outra memória.' : r.action === 'duplicate' ? 'Já estava guardada.' : 'Guardada.'));
      if (out) { newText.value = ''; load(); }
    })), row(btn('Consolidar duplicatas', () => act(() => api('/memory/consolidate', { method: 'POST' }), (r) => `${r.merged} memórias consolidadas.`)), btn('Importar do Odysseus', () => act(() => api('/memory/import-odysseus', { method: 'POST' }), (r) => `${r.created} importadas, ${r.duplicates} já existiam.`).then(load)))),
    candidates.candidates.length ? card('Aguardando sua revisão', candidates.candidates.map(candidateRow), sub('Conversa nunca vira fato sozinha, exceto quando você pede "lembre que...".')) : null,
    card('Explorar', row(query, kind, h('label', { class: 'jarvis-check' }, hypo, ' Incluir hipóteses'), btn('Buscar', load)), list),
  );
  await load();
}

// ---- Aprovações ----
async function renderAprovacoes(root) {
  const pending = (await api('/approvals')).approvals;
  const audit = (await api('/audit?limit=40')).events;
  const riskPt = { read: 'leitura', reversible: 'reversível', external: 'externa', destructive: 'destrutiva', financial: 'financeira', physical_critical: 'física crítica' };
  root.append(
    card('Aguardando aprovação', pending.length ? pending.map((a) => {
      const line = row(h('div', { class: 'grow' }, a.summary, h('div', {}, badge(riskPt[a.risk] || a.risk, 'warn'), ' ', sub(`expira em ${fmtDate(a.expires_at)}`))),
        btn('Aprovar', async () => { const r = await act(() => api(`/approvals/${a.id}`, { json: { approve: true } }), (x) => (x.status === 'failed' ? `Falhou: ${x.result}` : 'Aprovado.')); if (r) line.remove(); }),
        btn('Rejeitar', async () => { if (await act(() => api(`/approvals/${a.id}`, { json: { approve: false } }), 'Rejeitado.')) line.remove(); }, 'delete'));
      return line;
    }) : empty('Nenhuma ação aguardando.')),
    card('Registro de ações', h('div', { class: 'jarvis-table' }, audit.map((e) => row(h('div', { class: 'grow jarvis-mono' }, e.action), e.outcome !== 'ok' ? badge(e.outcome, 'err') : null, badge(riskPt[e.risk] || e.risk), h('div', { class: 'admin-toggle-sub' }, fmtDate(e.ts)))))),
  );
}

// ---- Missões ----
async function renderMissoes(root) {
  const data = await api('/missions');
  const goal = input({ placeholder: 'Objetivo, ex.: toda segunda organize minhas tarefas', 'aria-label': 'Objetivo' });
  const cron = input({ placeholder: 'Cron opcional, ex.: 0 8 * * 1', 'aria-label': 'Agendamento', style: 'max-width:200px' });
  root.append(
    card('Nova missão', row(goal, cron, btn('Planejar', async () => { if (await act(() => api('/missions/plan', { json: { goal: goal.value, cron: cron.value || null } }), 'Missão criada.')) render(); })),
      sub('Um modelo propõe os passos, mas só ações do catálogo podem rodar, cada uma com seu nível de risco.')),
    card('Rotinas recomendadas', btn('Ativar o que se aplica', async () => {
      const r = await act(() => api('/routines', { method: 'POST' }));
      if (!r) return;
      feedback(r.created.length ? `Ativadas: ${r.created.join(', ')}.` : 'Nada novo para ativar.');
      render();
    }), sub('Backup criptografado, consolidação de memória e sincronizações do Google que estiverem conectadas. Nada aqui envia mensagens.')),
    card('Missões', data.missions.length ? data.missions.map((m) => h('div', { class: 'jarvis-mission' },
      row(h('div', { class: 'grow' }, m.title, m.cron ? sub(`Agendada (${m.cron}); próxima: ${fmtDate(m.next_run_at)}; ${m.run_count} execuções`) : null), statusBadge(m.status),
        !['cancelled', 'completed', 'failed'].includes(m.status) ? btn('Cancelar', async () => { if (await act(() => api(`/missions/${m.id}`, { method: 'DELETE' }), 'Cancelada.')) render(); }, 'delete') : null),
      h('div', { class: 'jarvis-steps' }, m.steps.map((s) => row(h('div', { class: 'grow admin-toggle-sub' }, `${s.idx + 1}. ${s.title}`, s.error ? ` (${s.error})` : ''), statusBadge(s.status)))))) : empty('Nenhuma missão. Tente no Comando: "Jarvis, todos os dias às 8 horas me informe meus compromissos".')),
  );
}

// ---- E-mail ----
function messageRow(m) {
  const detail = h('div', { class: 'jarvis-answer' });
  const show = (text) => { detail.textContent = text; };
  return h('div', { class: 'jarvis-mission' },
    row(h('div', { class: 'grow' }, m.subject || '(sem assunto)', sub(`${m.from} · ${m.snippet}`)),
      btn('Resumir', async () => {
        show('Resumindo...');
        const r = await act(() => api(`/gmail/messages/${m.id}/summary`, { method: 'POST' }));
        show(r?.summary || '');
      })),
    (m.attachments || []).filter((a) => a.attachment_id).map((a) => row(h('div', { class: 'grow admin-toggle-sub' }, a.filename), btn('Ler anexo', async () => {
      const r = await act(() => api(`/gmail/messages/${m.id}/attachments/${encodeURIComponent(a.attachment_id)}`));
      if (r) show(r.supported ? r.text : `Formato não suportado: ${r.mime}`);
    }))),
    detail);
}

async function renderEmail(root) {
  const [insights, queue] = await Promise.all([api('/gmail/insights').catch(() => ({ insights: [] })), api('/gmail/outbox').catch(() => ({ queue: [] }))]);
  const out = h('div', { class: 'jarvis-table' });
  const to = input({ placeholder: 'Para', 'aria-label': 'Para' });
  const subject = input({ placeholder: 'Assunto', 'aria-label': 'Assunto' });
  const body = h('textarea', { class: 'jarvis-input', rows: 4, placeholder: 'Mensagem', 'aria-label': 'Mensagem' });
  const when = h('input', { class: 'jarvis-input', type: 'datetime-local', 'aria-label': 'Enviar em' });
  const q = input({ placeholder: 'Buscar por texto', 'aria-label': 'Buscar e-mails', onkeydown: (e) => { if (e.key === 'Enter') doSearch(); } });
  const results = h('div', { class: 'jarvis-table' });
  const doSearch = async () => {
    const data = await act(() => api(`/gmail/search?text=${encodeURIComponent(q.value)}`));
    if (data) results.replaceChildren(...(data.messages.length ? data.messages.map(messageRow) : [empty('Nada encontrado.')]));
  };
  const payload = () => ({ to: to.value.split(',').map((x) => x.trim()).filter(Boolean), subject: subject.value, body: body.value, send_at: when.value || null, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone });
  root.append(
    card('Triagem', row(btn('Classificar novos', async () => { const r = await act(() => api('/gmail/triage', { method: 'POST' }), (x) => `${x.processed} novas, ${x.already_seen} já vistas.`); if (r) { out.replaceChildren(...r.messages.map((m) => row(h('div', { class: 'grow' }, m.subject, sub(m.from)), badge(m.category), m.important ? badge('importante', 'warn') : null, m.suspected_spam ? badge('suspeito', 'err') : null))); } })), out,
      sub('A triagem nunca envia nada. Instruções dentro de e-mails são tratadas como texto.')),
    card('Buscar', row(q, btn('Buscar', doSearch)), results),
    card('Escrever', row(to, subject), body, row(when, btn('Salvar rascunho', () => act(() => api('/gmail/drafts', { json: payload() }), 'Rascunho criado.')), btn('Agendar envio', async () => { const r = await act(() => api('/gmail/outbox', { json: payload() }), (x) => (x.status === 'awaiting_approval' ? 'Na fila, aguardando sua aprovação.' : 'Na fila.')); if (r) render(); })), sub('O envio passa pela aprovação, salvo regra sua em contrário, e nunca sai duas vezes.')),
    card('Fila de envio', queue.queue.length ? queue.queue.map((m) => row(h('div', { class: 'grow' }, m.subject, sub(`${m.to.join(', ')} · ${fmtDate(m.send_at)}`), m.error ? sub(m.error) : null), statusBadge(m.status), ['queued', 'awaiting_approval'].includes(m.status) ? btn('Cancelar', async () => { if (await act(() => api(`/gmail/outbox/${m.id}`, { method: 'DELETE' }), 'Cancelado.')) render(); }, 'delete') : null)) : empty('Fila vazia.')),
    card('Últimas classificações', insights.insights.length ? h('div', { class: 'jarvis-table' }, insights.insights.slice(0, 20).map((i) => row(h('div', { class: 'grow admin-toggle-sub' }, (i.action_items[0] || i.message_id)), badge(i.category), i.important ? badge('importante', 'warn') : null))) : empty('Nada classificado ainda.')),
  );
}

// ---- Documentos ----
async function pickPhotos(gallery) {
  const session = await act(() => api('/photos/sessions', { method: 'POST' }));
  if (!session?.picker_uri) return;
  window.open(session.picker_uri, '_blank', 'noopener');
  gallery.replaceChildren(sub('Aguardando sua seleção na janela do Google...'));
  const started = Date.now();
  const interval = Math.max(Number.parseFloat(session.poll?.pollInterval) || 3, 3) * 1000;
  while (Date.now() - started < 300000 && _open) {
    await new Promise((resolve) => { setTimeout(resolve, interval); });
    const state = await api(`/photos/sessions/${session.session_id}`).catch(() => null);
    if (state?.ready) {
      renderGallery(gallery, session.session_id, state.items);
      return;
    }
  }
  gallery.replaceChildren(sub('Seleção não concluída.'));
}

function renderGallery(gallery, sessionId, items) {
  if (!items.length) { gallery.replaceChildren(sub('Nenhuma foto selecionada.')); return; }
  gallery.replaceChildren(h('div', { class: 'jarvis-gallery' }, items.map((item) => {
    const note = h('div', { class: 'admin-toggle-sub' });
    return h('figure', { class: 'jarvis-photo' },
      item.mime?.startsWith('image/') ? h('img', { src: `${API}/photos/sessions/${sessionId}/items/${encodeURIComponent(item.id)}/image?size=256`, alt: item.filename || 'Foto', loading: 'lazy' }) : sub(item.filename || 'Vídeo'),
      item.mime?.startsWith('image/') ? btn('Analisar', async () => {
        note.textContent = 'Analisando...';
        const r = await act(() => api('/photos/analyze', { json: { session_id: sessionId, item_id: item.id } }));
        note.textContent = r?.description || '';
      }) : null,
      note);
  })));
}

async function renderDocumentos(root) {
  const gallery = h('div', {});
  const allowed = await api('/drive/allowed');
  const folders = h('textarea', { class: 'jarvis-input', rows: 2, placeholder: 'IDs de pasta do Drive, um por linha', 'aria-label': 'Pastas autorizadas' }, allowed.folder_ids.join('\n'));
  const q = input({ placeholder: 'Ex.: contrato mais recente', 'aria-label': 'Buscar documentos', onkeydown: (e) => { if (e.key === 'Enter') doSearch(); } });
  const results = h('div', { class: 'jarvis-table' });
  const doSearch = async () => {
    const data = await act(() => api(`/drive/search?q=${encodeURIComponent(q.value)}`));
    if (data) results.replaceChildren(...(data.results.length ? data.results.map((d) => h('div', { class: 'jarvis-turn' }, h('strong', {}, d.file), sub(d.text))) : [empty('Nada encontrado nos documentos indexados.')]));
  };
  root.append(
    card('Pastas que podem alimentar a memória', folders, row(btn('Salvar', () => act(() => api('/drive/allowed', { method: 'PUT', json: { folder_ids: folders.value.split('\n').map((x) => x.trim()).filter(Boolean) } }), 'Pastas atualizadas.')), btn('Sincronizar agora', () => act(() => api('/drive/sync', { method: 'POST' }), (r) => `${r.indexed} indexados, ${r.unchanged} sem mudança.`))), sub('Nada é indexado sem estar nesta lista. Arquivos que saem dela são removidos do índice.')),
    card('Buscar', row(q, btn('Buscar', doSearch)), results),
    card('Google Fotos', btn('Escolher fotos', () => pickPhotos(gallery)), gallery, sub('Você escolhe no seletor do Google, e o Jarvis só vê essas fotos. Desde 2025 o Google não dá acesso à biblioteca inteira. As imagens não são guardadas.')),
  );
}

// ---- Integrações ----
async function renderIntegracoes(root) {
  const google = await api('/google/accounts');
  const groupsPt = { calendar: 'Agenda', gmail: 'Gmail', drive: 'Drive', photos: 'Fotos' };
  const choices = google.groups.map((g) => h('label', { class: 'jarvis-check' }, h('input', { type: 'checkbox', value: g }), ` ${groupsPt[g] || g}`));
  const storage = await api('/storage');
  const cfg = await api('/config');
  root.append(
    card('Google', google.configured ? null : sub('Defina GOOGLE_OAUTH_CLIENT_ID e GOOGLE_OAUTH_CLIENT_SECRET no .env. Passo a passo em docs/integrations/google-oauth.md.'),
      google.accounts.map((a) => row(h('div', { class: 'grow' }, a.email, sub(a.groups.map((g) => groupsPt[g] || g).join(', ') || 'apenas identidade')), statusBadge(a.status), btn('Revogar', async () => { if (await act(() => api(`/google/accounts/${a.id}`, { method: 'DELETE' }), 'Acesso revogado e tokens apagados.')) render(); }, 'delete'))),
      row(choices), btn('Conectar ou ampliar permissões', async () => {
        const groups = choices.map((c) => c.querySelector('input')).filter((i) => i.checked).map((i) => i.value);
        const r = await act(() => api('/google/connect', { json: { groups } }));
        if (r?.url) window.location.href = r.url;
      }, 'add', { disabled: !google.configured }), btn('Sincronizar agenda', () => act(() => api('/google/calendar/sync', { method: 'POST' }), (r) => `${r.created} novos, ${r.updated} atualizados, ${r.removed} removidos.`))),
    card('Armazenamento', h('div', { class: 'jarvis-chips' }, badge(`Backend: ${storage.backend}`), badge(`R2: ${storage.r2_configured ? 'configurado' : 'não configurado'}`, storage.r2_configured ? 'ok' : null), badge(`Classe A no mês: ${storage.usage.class_a}`), badge(`Classe B no mês: ${storage.usage.class_b}`)), sub('Limites gratuitos do R2: 1 milhão de operações classe A e 10 milhões de classe B por mês. O Jarvis para antes de ultrapassá-los.')),
    card('Firebase (opcional)', h('div', { class: 'jarvis-chips' }, badge(cfg.firebase.enabled ? 'réplica ligada' : 'réplica desligada', cfg.firebase.enabled ? 'ok' : null), badge(`Pendentes: ${storage.replication.pending}`), storage.replication.stuck ? badge(`Com erro: ${storage.replication.stuck}`, 'err') : null), sub('O SQLite continua sendo a fonte da verdade. O Firestore recebe uma cópia, com o texto das memórias criptografado por padrão.')),
  );
}

// ---- Dispositivos ----
async function renderDispositivos(root) {
  const data = await api('/home');
  const url = input({ placeholder: 'URL do Home Assistant', value: data.url, 'aria-label': 'URL' });
  const token = input({ placeholder: data.configured ? 'Token salvo; preencha só para trocar' : 'Token de acesso de longa duração', type: 'password', 'aria-label': 'Token', autocomplete: 'off' });
  const name = input({ placeholder: 'Nome, ex.: Televisão', 'aria-label': 'Nome' });
  const entity = input({ placeholder: 'media_player.tv_sala', 'aria-label': 'Entidade' });
  const area = input({ placeholder: 'Cômodo', 'aria-label': 'Cômodo' });
  const critical = h('input', { type: 'checkbox' });
  const OPS = { light: ['on', 'off'], switch: ['on', 'off'], media_player: ['on', 'off'], fan: ['on', 'off'], climate: ['on', 'off'], cover: ['open', 'close'], lock: ['lock', 'unlock'], alarm_control_panel: ['arm', 'disarm'] };
  const OPS_PT = { on: 'Ligar', off: 'Desligar', open: 'Abrir', close: 'Fechar', lock: 'Trancar', unlock: 'Destrancar', arm: 'Armar', disarm: 'Desarmar' };
  root.append(
    card('Home Assistant', row(url, token, btn('Salvar', async () => { if (await act(() => api('/home/config', { method: 'PUT', json: { url: url.value, token: token.value || null } }), 'Configuração salva.')) render(); })), row(badge(data.configured ? 'configurado' : 'não configurado', data.configured ? 'ok' : null), btn('Descobrir dispositivos', async () => { if (await act(() => api('/home/discover', { method: 'POST' }), (r) => `${r.discovered} dispositivos encontrados.`)) render(); }))),
    card('Adicionar manualmente', row(name, entity, area), row(h('label', { class: 'jarvis-check' }, critical, ' Risco físico: sempre pedir aprovação'), btn('Adicionar', async () => { if (await act(() => api('/home/devices', { json: { name: name.value, entity_id: entity.value, area: area.value, critical: critical.checked } }), 'Adicionado.')) render(); }))),
    card('Dispositivos', data.devices.length ? data.devices.map((d) => row(h('div', { class: 'grow' }, d.name, sub(`${d.area ? `${d.area} · ` : ''}${d.entity_id}`)), d.critical ? badge('crítico', 'warn') : null, (OPS[d.domain] || []).map((op) => btn(OPS_PT[op], () => act(() => api('/home/control', { json: { device_id: d.id, op } }), 'Comando enviado.'))),
      ['sensor', 'binary_sensor'].includes(d.domain) ? btn('Ler', async () => { const s = await act(() => api(`/home/devices/${d.id}/state`)); if (s) feedback(`${d.name}: ${s.state}${s.unit ? ` ${s.unit}` : ''}`); }) : null,
      d.domain === 'camera' ? btn('Ver agora', () => { window.open(`${API}/home/devices/${d.id}/snapshot`, '_blank', 'noopener'); }) : null,
      btn('Remover', async () => { if (await act(() => api(`/home/devices/${d.id}`, { method: 'DELETE' }))) render(); }, 'delete'))) : empty('Nenhum dispositivo.'), sub('Fechaduras, portões e alarmes sempre pedem aprovação, independentemente de qualquer regra ou modelo.')),
  );
}

// ---- Monitor ----
async function renderMonitor(root) {
  const m = await api('/monitor');
  const r = m.resources;
  const dict = (obj) => Object.entries(obj || {}).map(([k, v]) => badge(`${STATUS_PT[k] || k}: ${v}`, statusTone[k]));
  root.append(
    card('Serviços', h('div', { class: 'jarvis-chips' }, m.services.map((s) => badge(`${s.name}: ${s.online ? 'online' : 'offline'}`, s.online ? 'ok' : (s.required ? 'err' : null))))),
    h('div', { class: 'jarvis-stats' }, stat('Carga (1 min)', `${r.load_1m} / ${r.cpu_count} CPUs`), stat('RAM', `${r.memory_pct ?? '?'}%`, r.memory_pct > 90 ? 'warn' : null), stat('Disco', `${r.disk_pct}%`, r.disk_pct > 90 ? 'err' : null), stat('Disco livre', fmtBytes(r.disk_free)), stat('Banco Odysseus', fmtBytes(m.databases.app_db_bytes)), stat('Banco Jarvis', fmtBytes(m.databases.jarvis_db_bytes))),
    r.gpu ? card('GPU', r.gpu.map((g) => row(h('div', { class: 'grow' }, g.name), badge(`${g.utilization_pct}%`), badge(`${g.memory_used_mb}/${g.memory_total_mb} MB`)))) : null,
    card('Modelos e custo', h('div', { class: 'jarvis-chips' }, badge(`${m.models.total} ${m.models.total === 1 ? 'modelo' : 'modelos'}`), dict(m.models.by_tier), badge(`US$ ${m.usage.month_cost_usd.toFixed(4)} no mês`, m.usage.budget_exceeded ? 'err' : null))),
    card('Trabalho em segundo plano', h('div', { class: 'jarvis-chips' }, badge(`Passos rodando: ${m.steps_running}`), dict(m.missions), dict(m.mail_queue)), m.sync.map((s) => row(h('div', { class: 'grow' }, s.resource), s.last_error ? badge(s.last_error, 'err') : badge(fmtDate(s.last_ok_at), 'ok')))),
  );
}

// ---- Backup ----
async function renderBackup(root) {
  const data = await api('/backups');
  root.append(
    card('Backup criptografado', data.key_configured ? null : sub('Para usar este botão, defina JARVIS_BACKUP_KEY_FILE. Pelo terminal: scripts/jarvis-backup create --key-file ~/jarvis-recovery.key'),
      btn('Criar backup agora', async () => { feedback('Criando backup...', 'warn'); if (await act(() => api('/backups', { json: { include_secrets: true } }), (r) => `Backup criado com ${r.files} arquivos.`)) render(); }, 'add', { disabled: !data.key_configured }),
      sub('Inclui bancos, memórias, vetores, configurações e segredos, tudo criptografado. A chave fica fora do repositório: sem ela, nenhum backup abre.')),
    card('Backups disponíveis', data.backups.length ? data.backups.map((b) => row(h('div', { class: 'grow jarvis-mono' }, b.name), badge(fmtBytes(b.size)), h('div', { class: 'admin-toggle-sub' }, fmtDate(new Date(b.modified * 1000).toISOString())))) : empty('Nenhum backup nesta pasta.')),
    card('Restaurar em outro computador', sub('git clone → scripts/jarvis-install → scripts/jarvis-backup restore --key-file CHAVE ARQUIVO --data-dir data --env-target .env → scripts/jarvis-backup validate --data-dir data. Veja docs/operations/restore.md.')),
  );
}

// ---- Ajustes ----
async function renderAjustes(root) {
  const cfg = await api('/config');
  const budget = input({ type: 'number', min: 0, step: 0.5, value: cfg.budget.monthly_usd, 'aria-label': 'Orçamento mensal', style: 'max-width:120px' });
  const address = input({ value: cfg.persona.address_as, placeholder: 'Como devo chamar você?', 'aria-label': 'Tratamento' });
  const toggle = (label, section, key, value) => h('label', { class: 'jarvis-check' }, h('input', { type: 'checkbox', checked: value, onchange: (e) => act(() => api('/config', { method: 'PATCH', json: { [section]: { [key]: e.target.checked } } }), 'Salvo.') }), ` ${label}`);
  const rules = cfg.autonomy.global || {};
  const ruleRow = (key, label) => row(h('div', { class: 'grow' }, label), h('select', { class: 'jarvis-input', 'aria-label': label, onchange: (e) => act(() => api('/config', { method: 'PATCH', json: { autonomy: { global: { [key]: e.target.value } } } }), 'Política atualizada.') }, [['confirm', 'Pedir aprovação'], ['auto', 'Executar direto'], ['deny', 'Bloquear']].map(([v, l]) => h('option', { value: v, selected: (rules[key] || (['read', 'reversible'].includes(key) ? 'auto' : 'confirm')) === v }, l))));
  root.append(
    card('Identidade', row(address, btn('Salvar', () => act(() => api('/config', { method: 'PATCH', json: { persona: { address_as: address.value } } }), 'Salvo.'))), toggle('Usar a personalidade Jarvis nas conversas', 'persona', 'enabled', cfg.persona.enabled), toggle('Responder comandos diretos sem usar modelo', 'intents', 'enabled', cfg.intents.enabled)),
    card('Autonomia', ruleRow('read', 'Somente leitura'), ruleRow('reversible', 'Alterações reversíveis'), ruleRow('external', 'Ações externas (mensagens, convites)'), sub('Ações destrutivas, financeiras e físicas críticas sempre pedem aprovação; nenhuma regra muda isso.')),
    card('Aprendizado', toggle('Aprender com as conversas', 'learning', 'enabled', cfg.learning.enabled), toggle('Guardar direto quando eu disser "lembre que..."', 'learning', 'auto_accept_explicit', cfg.learning.auto_accept_explicit), toggle('Usar a memória nas respostas', 'memory', 'inject_in_chat', cfg.memory.inject_in_chat)),
    card('Orçamento', row(budget, btn('Salvar', () => act(() => api('/config', { method: 'PATCH', json: { budget: { monthly_usd: Number(budget.value) } } }), 'Orçamento salvo.'))), sub('Zero significa sem limite. Ao atingir o limite, modelos pagos saem dos fallbacks. Só conta chamadas com preço conhecido.')),
  );
}

const TABS = [
  ['painel', 'Painel', renderPainel], ['voz', 'Comando e voz', renderVoz], ['aprovacoes', 'Aprovações', renderAprovacoes], ['memoria', 'Memória', renderMemoria],
  ['modelos', 'Modelos', renderModelos], ['missoes', 'Missões', renderMissoes], ['email', 'E-mail', renderEmail], ['documentos', 'Documentos', renderDocumentos],
  ['integracoes', 'Integrações', renderIntegracoes], ['dispositivos', 'Dispositivos', renderDispositivos], ['monitor', 'Monitor', renderMonitor], ['backup', 'Backup', renderBackup], ['ajustes', 'Ajustes', renderAjustes],
];

async function render() {
  const body = document.getElementById('jarvis-body');
  if (!body) return;
  const token = ++_renderToken;
  const content = h('div', { class: 'jarvis-tab-content' });
  body.replaceChildren(h('div', { class: 'admin-toggle-sub jarvis-empty', role: 'status' }, 'Carregando...'));
  document.querySelectorAll('#jarvis-tabs .admin-tab').forEach((t) => {
    const active = t.dataset.tab === _tab;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', String(active));
  });
  const entry = TABS.find(([id]) => id === _tab) || TABS[0];
  try {
    await entry[2](content);
  } catch (err) {
    if (token !== _renderToken) return;
    const message = err.status === 403 ? 'Esta área é restrita a administradores.' : err.status === 401 ? 'Sessão expirada.' : `Não foi possível carregar: ${err.message}`;
    body.replaceChildren(h('div', { class: 'admin-toggle-sub jarvis-empty', role: 'alert' }, message), btn('Tentar de novo', render));
    return;
  }
  if (token === _renderToken) body.replaceChildren(content);
}

function buildModal() {
  const modal = h('div', { id: 'jarvis-modal', class: 'modal hidden', onclick: (e) => { if (e.target.id === 'jarvis-modal') closeJarvis(); } },
    h('div', { class: 'modal-content jarvis-modal-content', role: 'dialog', 'aria-label': 'J.A.R.V.I.S.', style: 'background:var(--bg)' },
      h('div', { class: 'modal-header' }, h('h4', {}, 'J.A.R.V.I.S.'), h('button', { class: 'close-btn', id: 'close-jarvis-modal', 'aria-label': 'Fechar', onclick: () => closeJarvis() }, '✖')),
      h('div', { id: 'jarvis-tabs', class: 'admin-tabs jarvis-tabs', role: 'tablist' }, TABS.map(([id, label]) => h('button', { class: 'admin-tab', role: 'tab', type: 'button', 'data-tab': id, onclick: () => { _tab = id; render(); } }, label))),
      h('div', { id: 'jarvis-feedback', class: 'jarvis-feedback', role: 'status', 'aria-live': 'polite' }),
      h('div', { class: 'modal-body jarvis-body', id: 'jarvis-body' })));
  document.body.append(modal);
  return modal;
}

function _onKey(e) { if (e.key === 'Escape' && _open) closeJarvis(); }

export function openJarvis(tab) {
  const modal = document.getElementById('jarvis-modal') || buildModal();
  if (tab) _tab = tab;
  modal.classList.remove('hidden');
  _open = true;
  document.addEventListener('keydown', _onKey);
  render();
}

export function closeJarvis() {
  document.getElementById('jarvis-modal')?.classList.add('hidden');
  _open = false;
  refreshAttention();
  document.removeEventListener('keydown', _onKey);
  closeMic();
  stopSpeaking();
}

export const isJarvisOpen = () => _open;

/** Attention dot on the nav button: actions and memories waiting for the user. */
async function refreshAttention() {
  try {
    const status = await api('/status');
    const pending = status.approvals_pending + status.learning_pending;
    for (const id of ['tool-jarvis-btn', 'rail-jarvis']) {
      const button = document.getElementById(id);
      if (!button) continue;
      button.classList.toggle('jarvis-attention', pending > 0);
      if (pending > 0) button.setAttribute('aria-description', `${pending} item(ns) aguardando você`);
      else button.removeAttribute('aria-description');
    }
  } catch { /* logged out or offline: try again on the next tick */ }
}

/** Adds "Automático" to the chat's model picker. Choosing a model by hand turns it off. */
async function initAutoRouting() {
  const menu = document.getElementById('model-picker-menu');
  const list = document.getElementById('model-picker-list');
  if (!menu || !list) return;
  const box = h('input', { type: 'checkbox', 'aria-label': 'Roteamento automático' });
  const rowEl = h('label', { class: 'jarvis-auto-row', title: 'O Jarvis escolhe o modelo a cada mensagem, seguindo as regras de custo' }, box, ' Automático (Jarvis)');
  list.parentNode.insertBefore(rowEl, list);
  let enabled = false;
  try { enabled = (await api('/routing')).auto; } catch { rowEl.remove(); return; }
  box.checked = enabled;
  box.addEventListener('change', async () => {
    const wanted = box.checked;
    try {
      await api('/routing', { method: 'PUT', json: { auto: wanted } });
      enabled = wanted;
    } catch { box.checked = enabled; }
  });
  document.addEventListener('odysseus:model-picked', async () => {
    if (!enabled) return;
    enabled = false;
    box.checked = false;
    await api('/routing', { method: 'PUT', json: { auto: false } }).catch(() => {});
  });
}

export function initJarvis() {
  initAutoRouting();
  refreshAttention();
  setInterval(refreshAttention, 60000);
  const params = new URLSearchParams(window.location.search);
  const result = params.get('jarvis');
  if (window.location.pathname === '/jarvis' || result) {
    openJarvis(result ? 'integracoes' : undefined);
    if (result) {
      setTimeout(() => feedback(result === 'google_connected' ? 'Conta Google conectada.' : 'A autorização do Google foi cancelada.', result === 'google_connected' ? 'ok' : 'warn'), 400);
      params.delete('jarvis');
      history.replaceState(null, '', window.location.pathname + (params.toString() ? `?${params}` : ''));
    }
  }
}

export default { openJarvis, closeJarvis, isJarvisOpen, initJarvis };
