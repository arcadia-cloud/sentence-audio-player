const app = document.getElementById('app');
const audio = document.getElementById('audio');
const toastEl = document.getElementById('toast');
const topMeta = document.getElementById('top-meta');

const state = {
  media: null, transcript: null, peaks: null,
  selected: -1, editor: -1, splitWord: -1,
  duration: 0, mode: 'idle', active: -1, loop: false,
  poll: null, waveBase: null, scrubbing: false, improve: false,
  selection: [], anchor: -1, selLoop: false, range: null, undoHistory: [],
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const fmtDur = (t) => {
  t = Math.max(0, t || 0);
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`;
};
const fmtShort = (t) => {
  t = Math.max(0, t || 0);
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
};

async function api(path, opt = {}) {
  const r = await fetch(path, opt);
  if (!r.ok) {
    const raw = await r.text();
    let msg = raw.slice(0, 400) || r.statusText;
    try {
      const data = JSON.parse(raw);
      if (data && data.detail) msg = String(data.detail);
    } catch (_) { /* keep raw text */ }
    throw new Error(msg);
  }
  return r.json();
}

let toastTimer = null;
function toast(msg, ms = 3200) {
  toastEl.textContent = msg;
  toastEl.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('on'), ms);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { /* ignore */ }
    ta.remove();
    return ok;
  }
}

function stopPoll() { if (state.poll) { clearInterval(state.poll); state.poll = null; } }function resetPlayback() {
  audio.pause();
  state.mode = 'idle';
  state.active = -1;
  state.selected = -1;
  state.editor = -1;
  state.splitWord = -1;
  state.selection = [];
  state.anchor = -1;
  state.range = null;
  state.selLoop = false;
  document.body.classList.remove('editor-open');
}

/* ------------------------------------------------------------------ routing */
window.addEventListener('hashchange', route);
function route() {
  stopPoll();
  resetPlayback();
  state.transcript = null;
  state.peaks = null;
  state.media = null;
  const h = location.hash;
  if (h.startsWith('#/w/')) openWorkbench(h.slice(4));
  else renderLibrary();
}

/* ------------------------------------------------------------------ library */
async function renderLibrary() {
  topMeta.textContent = '';
  app.innerHTML = `
    <section class="hero">
      <div class="hero-row">
        <div>
          <h1>听力材料工作台</h1>
          <p>上传视频或音频，自动分离音轨、转写英文，并把每一句对齐到音频。点击句子即可复听。</p>
        </div>
        <button id="storage-btn" class="ghost">数据目录</button>
      </div>
      <div class="dropzone" id="drop">
        <strong>拖入文件，或点击选择</strong>
        支持 mp4 / mov / mkv / mp3 / wav / m4a / flac，单个文件建议 30 分钟以内
        <div class="upload-bar" id="ubar"><i></i></div>
      </div>
      <input type="file" id="file" accept="video/*,audio/*" hidden>
    </section>
    <div class="media-grid" id="media-grid"><div class="empty">加载中…</div></div>`;

  const drop = $('#drop'), input = $('#file');
  drop.onclick = () => input.click();
  input.onchange = () => input.files[0] && handleFile(input.files[0]);
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('drag'); }));
  drop.addEventListener('drop', (e) => { const f = e.dataTransfer.files[0]; if (f) handleFile(f); });
  $('#storage-btn').onclick = openStorage;

  try {
    const [media, cfg] = await Promise.all([api('/api/media'), api('/api/config')]);
    topMeta.textContent = `${cfg.model} · ${cfg.device} · ${cfg.language}`;
    renderMediaGrid(media);
  } catch (e) {
    $('#media-grid').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

function statusTag(m) {
  if (m.status === 'ready') return `<span class="tag ready">已就绪</span>`;
  if (m.status === 'transcribing' || m.status === 'queued') return `<span class="tag transcribing">转写中</span>`;
  if (m.status === 'error') return `<span class="tag error">失败</span>`;
  return `<span class="tag">未转写</span>`;
}

function renderMediaGrid(media) {
  const grid = $('#media-grid');
  if (!media.length) { grid.innerHTML = `<div class="empty">还没有材料，先上传一个吧。</div>`; return; }
  grid.innerHTML = media.map((m) => `
    <div class="card" data-id="${m.id}">
      <h3 title="${esc(m.title)}">${esc(m.title)}</h3>
      <div class="meta">${fmtShort(m.duration)} · ${m.seg_count || 0} 句 · ${statusTag(m)}</div>
      <div class="actions">
        <button class="primary" data-open>打开</button>
        <button data-rename title="重命名">✎ 重命名</button>
        <button class="danger" data-del>删除</button>
      </div>
    </div>`).join('');
  grid.onclick = async (e) => {
    const card = e.target.closest('.card'); if (!card) return;
    const id = card.dataset.id;
    if (e.target.closest('[data-del]')) {
      e.stopPropagation();
      if (!await askConfirm('删除材料', '删除这个材料及其转写？此操作不可恢复。', '删除')) return;
      await api(`/api/media/${id}`, { method: 'DELETE' });
      renderLibrary();
      return;
    }
    if (e.target.closest('[data-rename]')) {
      e.stopPropagation();
      const title = await askText('重命名素材', card.querySelector('h3').textContent);
      if (title) {
        await api(`/api/media/${id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title }),
        });
        renderLibrary();
      }
      return;
    }
    location.hash = `#/w/${id}`;
  };
}

async function handleFile(file) {
  const bar = $('#ubar'), fill = bar?.querySelector('i');
  bar?.classList.add('on');
  const fd = new FormData();
  fd.append('file', file);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/media');
  xhr.upload.onprogress = (e) => { if (e.lengthComputable && fill) fill.style.width = `${e.loaded / e.total * 100}%`; };
  xhr.onload = async () => {
    if (xhr.status >= 400) { toast('上传失败：' + xhr.responseText.slice(0, 200)); bar?.classList.remove('on'); return; }
    const m = JSON.parse(xhr.responseText);
    try { await api(`/api/media/${m.id}/transcribe`, { method: 'POST' }); } catch (err) { toast(err.message); }
    location.hash = `#/w/${m.id}`;
  };
  xhr.onerror = () => { toast('上传失败，请检查文件。'); bar?.classList.remove('on'); };
  xhr.send(fd);
}

/* --------------------------------------------------------------- workbench */
async function openWorkbench(id) {
  stopPoll();
  resetPlayback();
  app.innerHTML = `<div class="empty">加载中…</div>`;
  let media;
  try { media = await api(`/api/media/${id}/status`); }
  catch (e) { app.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; return; }
  state.media = media;

  if (media.status === 'ready') { buildShell(media); await loadTranscript(id); return; }
  if (media.status === 'uploaded') { buildStart(media); return; }
  if (media.status === 'error') { buildError(media); return; }
  buildProcessing(media);
  pollStatus(id);
}

const QUALITY_LABELS = { fast: '快速', standard: '标准', high: '高精度' };
const QUALITY_MODEL = { fast: 'small.en', standard: 'medium.en', high: 'large-v3' };
const QUALITY_OPTIONS = `
  <option value="fast">快速 · small.en</option>
  <option value="standard">标准 · medium.en</option>
  <option value="high">高精度 · large-v3</option>`;

function workHead(media, extra = '') {
  const q = media.quality || 'fast';
  return `
    <div class="wb-head">
      <button id="back" class="ghost">← 素材库</button>
      <h1 id="wb-title" title="${esc(media.title)}">${esc(media.title)}</h1>
      <button id="rename-btn" class="icon" title="重命名">✎</button>
      <span class="meta" id="wb-meta"></span>
      <select id="quality" class="quality" title="转写精度">${QUALITY_OPTIONS}</select>
      <button id="retrans" class="ghost" title="用所选精度重新转写整篇">重转</button>
      <button id="storage-btn" class="ghost" title="数据目录">数据</button>
      ${extra}
    </div>`;
}

function bindHead(media) {
  $('#back').onclick = async () => { await closeEditorWithSave(); location.hash = '#/'; };
  $('#storage-btn').onclick = openStorage;
  const qsel = $('#quality');
  if (qsel) qsel.value = media.quality || 'fast';
  const imp = $('#improve');
  if (imp) {
    imp.checked = state.improve;
    imp.onchange = () => { state.improve = imp.checked; };
  }
  const retrans = $('#retrans');
  if (retrans) retrans.onclick = async () => {
    const quality = $('#quality').value;
    const label = QUALITY_LABELS[quality] || quality;
    const improve = $('#improve')?.checked ?? state.improve;
    if (!await askConfirm('重新转写整篇', `用「${label}」重新转写整篇，当前文本会被覆盖。`, '开始重转')) return;
    try {
      await api(`/api/media/${media.id}/transcribe`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ quality, improve }),
      });
      state.media = { ...state.media, quality };
      openWorkbench(media.id);
    } catch (e) { toast(e.message); }
  };
  $('#rename-btn').onclick = async () => {
    const title = await askText('重命名素材', state.media?.title || media.title);
    if (!title) return;
    const m = await api(`/api/media/${media.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    });
    state.media = { ...state.media, ...m };
    $('#wb-title').textContent = m.title;
    toast('已重命名');
  };
}

function buildStart(media) {
  app.innerHTML = `${workHead(media)}
    <div class="panel">
      <p>${fmtShort(media.duration)} · 已提取音轨，等待转写。</p>
      <p style="margin-top:12px">
        <label class="improve"><input type="checkbox" id="improve"> 低置信句自动重跑（更高一档模型）</label>
      </p>
      <p style="margin-top:12px"><button class="primary" id="go">开始转写</button></p>
    </div>`;
  bindHead(media);
  $('#go').onclick = async () => {
    const quality = $('#quality').value;
    const improve = $('#improve')?.checked ?? false;
    try {
      await api(`/api/media/${media.id}/transcribe`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ quality, improve }),
      });
    }
    catch (e) { toast(e.message); }
    openWorkbench(media.id);
  };
}

function buildError(media) {
  app.innerHTML = `${workHead(media)}
    <div class="panel">
      <p class="bad">转写失败：${esc(media.error || '未知错误')}</p>
      <p style="margin-top:14px">
        <button class="primary" id="retry">重试</button>
        <button id="back2">返回素材库</button>
      </p>
    </div>`;
  bindHead(media);
  $('#retry').onclick = async () => {
    const quality = $('#quality').value;
    try {
      await api(`/api/media/${media.id}/transcribe`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ quality }),
      });
    } catch (e) { toast(e.message); }
    openWorkbench(media.id);
  };
  $('#back2').onclick = () => { location.hash = '#/'; };
}

function buildProcessing(media) {
  app.innerHTML = `${workHead(media)}
    <div class="progress-card">
      <div class="spinner"></div>
      <div class="progress-title">正在处理音频</div>
      <div class="progress-stage" id="stage">解码音频…</div>
      <div class="progress-bar"><i id="prfill"></i></div>
      <div class="progress-hint">转写 + 分句 + 局部校对，22 分钟材料约 2 分钟</div>
    </div>`;
  bindHead(media);
}

function pollStatus(id) {
  stopPoll();
  state.poll = setInterval(async () => {
    try {
      const m = await api(`/api/media/${id}/status`);
      const stage = $('#stage'), fill = $('#prfill');
      if (m.status === 'ready') { stopPoll(); openWorkbench(id); return; }
      if (m.status === 'error') { stopPoll(); buildError(m); return; }
      if (stage) stage.textContent = m.stage || (m.status === 'queued'
        ? '排队中…'
        : (m.progress >= 0.9 ? '正在校对分句…' : m.progress >= 0.55 ? '正在分句…' : '正在转写…'));
      if (fill) fill.style.width = `${Math.round((m.progress || 0) * 100)}%`;
    } catch (e) { /* keep polling */ }
  }, 1200);
}

function buildShell(media) {
  app.innerHTML = `
    ${workHead(media, '<a href="/api/media/' + media.id + '/export?fmt=srt" download><button class="ghost">导出 SRT</button></a><a href="/api/media/' + media.id + '/export?fmt=txt" download><button class="ghost">TXT</button></a>')}
    <div class="wave-wrap">
      <canvas id="wave"></canvas>
      <div class="wave-foot"><span id="w-left">00:00</span><span id="w-right">00:00</span></div>
    </div>
    <div class="toolbar">
      <div class="undo-wrap">
        <button id="t-undo" title="撤销最近一次编辑（改文本/拆分/合并/核对/重转写）">↶ 撤销编辑 ▾</button>
        <div class="undo-pop" id="undo-pop" hidden></div>
      </div>
      <button id="improve-btn" title="用更高一档模型重跑低置信句">↺ 低置信句重跑</button>
      <div class="sel-bar" id="sel-bar" hidden>
        <span id="sel-label"></span>
        <button id="sel-play">▶ 播放选区</button>
        <label class="loop"><input type="checkbox" id="sel-loop"> 循环</label>
        <button id="sel-clear">清除</button>
      </div>
      <span class="spacer"></span>
      <span class="muted" id="t-hint">点击句子播放 · Shift+点击选连续多句 · 空格暂停</span>
    </div>
    <div id="seg-list" class="seg-list"><div class="empty">加载转写…</div></div>
    <div class="player" id="player">
      <button id="p-prev" class="icon">⏮</button>
      <button id="p-play" class="play">▶</button>
      <button id="p-next" class="icon">⏭</button>
      <span class="time" id="p-time">00:00 / 00:00</span>
      <div class="bar" id="p-bar"><i id="p-fill"></i></div>
      <div class="speed" id="speed">
        <button data-rate="0.75">0.75×</button><button data-rate="1" class="on">1×</button>
        <button data-rate="1.25">1.25×</button><button data-rate="1.5">1.5×</button><button data-rate="2">2×</button>
      </div>
      <label class="loop"><input type="checkbox" id="p-loop"> 循环本句</label>
    </div>
    <aside class="editor" id="editor">
      <div class="ed-head">
        <b>编辑句子</b><span id="ed-time" class="muted"></span>
        <button id="ed-close" class="icon">✕</button>
      </div>
      <div class="ed-body">
        <textarea id="ed-text" spellcheck="false"></textarea>
        <div class="ed-label">点一个词设为拆分点（当前：<span id="ed-split-label">无</span>）</div>
        <div class="ed-words" id="ed-words"></div>
        <div class="ed-grid">
          <button id="ed-play">▶ 试听本句</button>
          <button id="ed-split">✂ 拆分</button>
          <button id="ed-merge">⇄ 合并下一句</button>
          <button id="ed-retrans">↺ 重转写</button>
          <button id="ed-verify">✓ 已核对</button>
          <button id="ed-nudge">结尾 +0.1s</button>
          <button id="ed-nudge-down">结尾 -0.1s</button>
          <button id="ed-undo">↶ 撤销上一步</button>
        </div>
        <div class="ed-actions">
          <button class="primary" id="ed-save">保存文本</button>
          <button id="ed-cancel">关闭</button>
        </div>
        <div class="ed-note">拆分需先点词；单句重转写用当前精度的模型；整篇可用右上角「重转」换精度；撤销只保留本次运行中的最近 20 步。</div>
      </div>
    </aside>`;
  bindHead(media);
  $('#t-undo').onclick = toggleUndoPop;
  $('#improve-btn').onclick = improveNow;
  $('#sel-play').onclick = playSelection;
  $('#sel-clear').onclick = clearSelection;
  $('#sel-loop').onchange = (e) => { state.selLoop = e.target.checked; };
  $('#p-prev').onclick = () => playSegment(Math.max(0, (state.active < 0 ? state.selected : state.active) - 1));
  $('#p-next').onclick = () => playSegment(Math.min((state.transcript?.segments.length || 1) - 1, (state.active < 0 ? state.selected : state.active) + 1));
  $('#p-play').onclick = togglePlay;
  $('#p-loop').onchange = (e) => { state.loop = e.target.checked; };
  $('#speed').onclick = (e) => {
    const b = e.target.closest('button[data-rate]'); if (!b) return;
    audio.playbackRate = +b.dataset.rate;
    $$('#speed button').forEach((x) => x.classList.toggle('on', x === b));
  };
  $('#p-bar').onclick = null;
  bindScrubber();
  $('#wave').onclick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    seek((e.clientX - r.left) / r.width * state.duration);
  };
  bindEditor();
  $('#seg-list').addEventListener('mousedown', (e) => {
    if (e.shiftKey && e.button === 0 && e.target.closest('.seg')) e.preventDefault();
  });
  window.addEventListener('resize', debounce(resizeWave, 200));
}

function bindScrubber() {
  const bar = $('#p-bar'); if (!bar) return;
  let wasPlaying = false, startX = 0, moved = false;

  const toTime = (e) => {
    if (!state.duration) return;
    const r = bar.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    audio.currentTime = ratio * state.duration;
    const fill = $('#p-fill'); if (fill) fill.style.width = `${ratio * 100}%`;
    const pt = $('#p-time');
    if (pt) pt.textContent = `${fmtShort(audio.currentTime)} / ${fmtShort(state.duration)}`;
    paintWave();
  };

  bar.addEventListener('pointerdown', (e) => {
    if (!state.duration) return;
    e.preventDefault();
    bar.setPointerCapture(e.pointerId);
    wasPlaying = !audio.paused;
    startX = e.clientX;
    moved = false;
    state.scrubbing = true;
    bar.classList.add('scrubbing');
    audio.pause();
    toTime(e);
  });

  bar.addEventListener('pointermove', (e) => {
    if (!state.scrubbing) return;
    if (Math.abs(e.clientX - startX) > 2) moved = true;
    toTime(e);
  });

  const end = (e) => {
    if (!state.scrubbing) return;
    state.scrubbing = false;
    bar.classList.remove('scrubbing');
    try { bar.releasePointerCapture(e.pointerId); } catch (_) { /* ignore */ }
    state.mode = 'continuous';
    if (!moved || wasPlaying) audio.play().catch(() => {});
  };
  bar.addEventListener('pointerup', end);
  bar.addEventListener('pointercancel', end);
}

function debounce(fn, ms) { let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; }

async function loadTranscript(id) {
  try {
    const [tr, pk] = await Promise.all([
      api(`/api/media/${id}/transcript`),
      api(`/api/media/${id}/peaks`),
    ]);
    state.transcript = tr;
    state.duration = tr.duration || state.media.duration;
    state.peaks = JSON.parse(pk.peaks || '[]');
    audio.src = `/api/media/${id}/audio`;
    const done = tr.segments.filter((s) => s.verified).length;
  $('#wb-meta').textContent = `${fmtShort(state.duration)} · ${tr.segments.length} 句 · 已核对 ${done}/${tr.segments.length} · ${tr.model || ''}`;
    $('#w-right').textContent = fmtShort(state.duration);
    renderSegments();
    resizeWave();
    startTick();
    updateUndoButton();
  } catch (e) {
    $('#seg-list').innerHTML = `<div class="empty">加载转写失败：${esc(e.message)}</div>`;
  }
}

function renderSegments() {
  const list = $('#seg-list');
  const segs = state.transcript?.segments || [];
  if (!segs.length) { list.innerHTML = `<div class="empty">没有识别到语音。</div>`; return; }
  list.innerHTML = segs.map((s) => {
    const joined = s.words.map((w) => w.text).join('').trim();
    const same = !s.words.length || joined === s.text.trim();
    const text = same
      ? s.words.map((w, wi) => `<span class="w${w.prob < 0.55 ? ' low' : ''}" data-w="${wi}" title="置信度 ${(w.prob * 100).toFixed(0)}%">${esc(w.text)}</span>`).join('')
      : esc(s.text);
    const avg = s.words.length ? s.words.reduce((a, w) => a + w.prob, 0) / s.words.length : 1;
    return `
      <div class="seg${s.verified ? ' verified' : ''}" data-i="${s.idx}" data-id="${s.id}">
        <div class="gutter"><span class="num">#${s.idx + 1}</span><span>${fmtDur(s.start)}</span><span>${(s.end - s.start).toFixed(1)}s</span></div>
        <div class="body">
          <div class="text${same ? '' : ' plain'}">${text}</div>
          <div class="sub">
            ${s.verified ? '<span class="badge verified">已核对</span>' : ''}
            ${avg < 0.6 ? '<span class="badge">低置信</span>' : ''}
            <span>conf ${avg.toFixed(2)}</span>
          </div>
        </div>
        <div class="row-actions">
          <button class="icon ed" title="编辑这一句">✎</button>
          <button class="icon vf" title="标记已核对">✓</button>
        </div>
      </div>`;
  }).join('');

  list.onclick = async (e) => {
    const seg = e.target.closest('.seg'); if (!seg) return;
    const idx = +seg.dataset.i;
    if (e.shiftKey) { e.preventDefault(); selectRange(idx); return; }
    if (e.target.closest('.ed')) { e.stopPropagation(); openEditor(idx); return; }
    if (e.target.closest('.vf')) { e.stopPropagation(); selectSegment(idx); toggleVerify(); return; }
    const wasEditing = state.editor >= 0;
    if (wasEditing && state.editor !== idx) await openEditor(idx);
    clearSelection();
    playSegment(idx);
  };
}

function selectSegment(idx) {
  state.selected = idx;
  $$('.seg').forEach((el) => el.classList.toggle('active', +el.dataset.i === idx));
  paintWave();
}

function updateSelBar() {
  const bar = $('#sel-bar'); if (!bar) return;
  if (!state.selection.length) { bar.hidden = true; return; }
  const a = Math.min(...state.selection), b = Math.max(...state.selection);
  bar.hidden = false;
  $('#sel-label').textContent = a === b ? `第 ${a + 1} 句` : `第 ${a + 1}–${b + 1} 句 · ${b - a + 1} 句`;
}

function selectRange(idx) {
  const segs = state.transcript?.segments || [];
  if (!segs[idx]) return;
  if (state.anchor < 0) state.anchor = state.active >= 0 ? state.active : idx;
  const a = Math.min(state.anchor, idx), b = Math.max(state.anchor, idx);
  state.selection = [];
  for (let i = a; i <= b; i += 1) state.selection.push(i);
  $$('.seg').forEach((el) => {
    const i = +el.dataset.i;
    el.classList.toggle('picked', i >= a && i <= b);
  });
  selectSegment(idx);
  updateSelBar();
}

function clearSelection() {
  state.selection = [];
  state.anchor = -1;
  $$('.seg.picked').forEach((el) => el.classList.remove('picked'));
  updateSelBar();
  if (state.mode === 'range') {
    audio.pause();
    state.mode = 'idle';
    state.range = null;
    setSelIcon(false);
  }
}

function setSelIcon(playing) {
  const b = $('#sel-play');
  if (b) b.textContent = playing ? '⏸ 暂停选区' : '▶ 播放选区';
}

function playSelection() {
  if (!state.selection.length) { toast('先按住 Shift 点两句，选一段。'); return; }
  if (state.mode === 'range' && !audio.paused) { audio.pause(); return; }
  const segs = state.transcript?.segments || [];
  const a = Math.min(...state.selection), b = Math.max(...state.selection);
  if (!segs[a]) return;
  state.range = { a, b };
  state.mode = 'range';
  state.active = a;
  whenReady(() => {
    audio.currentTime = Math.max(0, segs[a].start - 0.04);
    audio.play().catch(() => {});
  });
  highlightRow(a);
  setSelIcon(true);
}

/* ------------------------------------------------------------------ editor */
function bindEditor() {
  $('#ed-close').onclick = closeEditorWithSave;
  $('#ed-cancel').onclick = closeEditorWithSave;
  $('#ed-play').onclick = () => { if (state.editor >= 0) playSegment(state.editor); };
  $('#ed-save').onclick = saveEditorText;
  $('#ed-split').onclick = editorSplit;
  $('#ed-merge').onclick = editorMerge;
  $('#ed-retrans').onclick = editorRetranscribe;
  $('#ed-verify').onclick = editorVerify;
  $('#ed-nudge').onclick = editorNudge;
  $('#ed-nudge-down').onclick = editorNudgeDown;
  $('#ed-undo').onclick = () => undoLast(1);
  $('#ed-words').onclick = (e) => {
    const w = e.target.closest('[data-w]'); if (!w) return;
    state.splitWord = +w.dataset.w;
    $$('#ed-words .w').forEach((x) => x.classList.toggle('sel', x === w));
    $('#ed-split').disabled = state.splitWord <= 0;
    $('#ed-split-label').textContent = state.splitWord > 0 ? `${state.splitWord}（在它前面拆）` : '无（不能在第 0 个词前拆）';
  };
}

async function openEditor(idx, force = false) {
  const s = state.transcript?.segments[idx]; if (!s) return;
  if (!force && state.editor === idx && $('#editor')?.classList.contains('open')) return;
  if (state.editor >= 0 && state.editor !== idx) await saveEditorSilently();
  audio.pause();
  state.mode = 'idle';
  state.editor = idx;
  state.splitWord = -1;
  selectSegment(idx);
  $('#ed-split').disabled = true;
  $('#ed-time').textContent = `#${idx + 1} · ${fmtDur(s.start)} – ${fmtDur(s.end)}`;
  $('#ed-text').value = s.text;
  $('#ed-split-label').textContent = '无';
  const joined = s.words.map((w) => w.text).join('').trim();
  const same = !s.words.length || joined === s.text.trim();
  $('#ed-words').innerHTML = same && s.words.length
    ? s.words.map((w, wi) => `<span class="w${w.prob < 0.55 ? ' low' : ''}" data-w="${wi}" title="${w.start.toFixed(2)}s">${esc(w.text)}</span>`).join('')
    : '<span class="muted">文本已被手动修改，词级锚点不可用；可重转写恢复。</span>';
  $('#ed-verify').textContent = s.verified ? '✓ 取消核对' : '✓ 标记已核对';
  document.body.classList.add('editor-open');
  $('#editor').classList.add('open');
}

function closeEditor() {
  state.editor = -1;
  state.splitWord = -1;
  document.body.classList.remove('editor-open');
  $('#editor')?.classList.remove('open');
}

async function saveEditorSilently() {
  const s = state.transcript?.segments[state.editor]; if (!s) return;
  const el = $('#ed-text'); if (!el) return;
  const text = el.value.trim();
  if (!text || text === s.text) return;
  try {
    await api(`/api/segments/${s.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    s.text = text;
  } catch (e) {
    toast('自动保存失败：' + e.message, 5000);
  }
}

async function closeEditorWithSave() {
  await saveEditorSilently();
  closeEditor();
}

async function saveEditorText() {
  const s = state.transcript?.segments[state.editor]; if (!s) return;
  const text = $('#ed-text').value.trim();
  if (!text) { toast('文本不能为空'); return; }
  const btn = $('#ed-save');
  btn.disabled = true;
  btn.textContent = '保存中…';
  btn.classList.remove('saved');
  try {
    await api(`/api/segments/${s.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    await reloadTranscript();
    btn.textContent = '已保存 ✓';
    btn.classList.add('saved');
    setTimeout(() => {
      if (!btn.isConnected) return;
      btn.textContent = '保存文本';
      btn.classList.remove('saved');
      btn.disabled = false;
    }, 1300);
  } catch (e) {
    btn.textContent = '保存文本';
    btn.disabled = false;
    toast('保存失败：' + e.message);
  }
}

async function editorSplit() {
  const s = state.transcript?.segments[state.editor]; if (!s) return;
  if (state.splitWord <= 0) { toast('先点一个词，再拆分。'); return; }
  await api(`/api/segments/${s.id}/split`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ word_index: state.splitWord }),
  });
  await reloadTranscript();
  openEditor(state.editor, true);
}

async function editorMerge() {
  const s = state.transcript?.segments[state.editor]; if (!s) return;
  await api(`/api/segments/${s.id}/merge`, { method: 'POST' });
  await reloadTranscript();
  openEditor(Math.min(state.editor, state.transcript.segments.length - 1), true);
}

async function editorRetranscribe() {
  const s = state.transcript?.segments[state.editor]; if (!s) return;
  const q = state.media?.quality || 'fast';
  const model = QUALITY_MODEL[q] || q;
  if (!await askConfirm('重转写这一句',
    `会用当前精度「${QUALITY_LABELS[q] || q}」的 ${model} 重新识别这一句，并覆盖当前文本。`, '重转写')) return;
  toast('正在重转写…');
  try {
    await api(`/api/segments/${s.id}/retranscribe`, { method: 'POST' });
    await reloadTranscript();
    openEditor(Math.min(state.editor, state.transcript.segments.length - 1), true);
    toast('已更新');
  } catch (e) { toast('重转写失败：' + e.message); }
}

async function editorVerify() {
  const s = state.transcript?.segments[state.editor]; if (!s) return;
  await api(`/api/segments/${s.id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ verified: !s.verified }),
  });
  await reloadTranscript();
  openEditor(Math.min(state.editor, state.transcript.segments.length - 1), true);
}

async function editorNudge() {
  const s = state.transcript?.segments[state.editor]; if (!s) return;
  await api(`/api/segments/${s.id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ end: Math.min(state.duration, s.end + 0.1) }),
  });
  await reloadTranscript();
  openEditor(Math.min(state.editor, state.transcript.segments.length - 1), true);
}

async function editorNudgeDown() {
  const s = state.transcript?.segments[state.editor]; if (!s) return;
  const end = Math.max(s.start + 0.15, s.end - 0.1);
  await api(`/api/segments/${s.id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ end }),
  });
  await reloadTranscript();
  openEditor(Math.min(state.editor, state.transcript.segments.length - 1), true);
}

async function toggleVerify() {
  const s = state.transcript?.segments[state.selected]; if (!s) return;
  await api(`/api/segments/${s.id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ verified: !s.verified }),
  });
  await reloadTranscript();
}

async function undoLast(steps = 1) {
  if (!state.media) return;
  if (steps > 1 && !await askConfirm('撤销多步编辑',
    `将撤销最近 ${steps} 步编辑，回到更早的版本。此操作不可重做。`, '撤销')) return;
  try {
    const r = await api(`/api/media/${state.media.id}/undo`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ steps }),
    });
    closeEditor();
    $('#undo-pop').hidden = true;
    await reloadTranscript();
    toast(r.steps > 1 ? `已撤销 ${r.steps} 步（回到：${r.label}）` : `已撤销：${r.label}`);
  } catch (e) { toast(e.message); }
}

function toggleUndoPop() {
  const pop = $('#undo-pop'); if (!pop) return;
  if (!pop.hidden) { pop.hidden = true; return; }
  renderUndoPop();
  pop.hidden = false;
}

function renderUndoPop() {
  const pop = $('#undo-pop'); if (!pop) return;
  const hist = state.undoHistory || [];
  if (!hist.length) { pop.hidden = true; return; }
  pop.innerHTML = `<div class="undo-head">最近编辑 · 点一项会撤销该项及之后的所有步骤</div>` +
    hist.map((h, i) => `
      <button class="undo-row" data-steps="${i + 1}">
        <span class="undo-act">${esc(h.label)}</span>
        <span class="undo-detail">${esc(h.detail || '')}</span>
        <span class="undo-time">${esc(h.time || '')}</span>
      </button>`).join('');
  pop.querySelectorAll('[data-steps]').forEach((b) => {
    b.onclick = () => undoLast(+b.dataset.steps);
  });
}

async function updateUndoButton() {
  if (!state.media) return;
  try {
    const info = await api(`/api/media/${state.media.id}/undo-info`);
    state.undoHistory = info.history || [];
    const b = $('#t-undo');
    if (!b) return;
    b.disabled = !info.count;
    b.title = info.count ? `最近一次：${info.label}` : '没有可撤销的编辑';
    if (!$('#undo-pop')?.hidden) renderUndoPop();
  } catch (_) { /* ignore */ }
}

async function improveNow() {
  if (!state.media) return;
  const q = state.media.quality || 'fast';
  const target = { fast: 'medium.en', standard: 'large-v3' }[q];
  if (!target) { toast('当前已经是最高精度'); return; }
  if (!await askConfirm('低置信句重跑',
    `将用 ${target} 重跑置信度低的句子（最多 12 句）。首次使用会下载模型，可能需要几分钟。`, '开始重跑')) return;
  const btn = $('#improve-btn');
  const old = btn?.textContent || '';
  if (btn) { btn.disabled = true; btn.textContent = '重跑中…'; }
  try {
    const r = await api(`/api/media/${state.media.id}/improve`, { method: 'POST' });
    await reloadTranscript();
    toast(r.total ? `已重跑 ${r.changed}/${r.total} 句` : '没有低置信句需要重跑', 5000);
  } catch (e) {
    toast('重跑失败：' + e.message, 7000);
  } finally {
    if (btn) { btn.textContent = old; btn.disabled = false; }
  }
}

async function reloadTranscript() {
  const keep = state.selected;
  const tr = await api(`/api/media/${state.media.id}/transcript`);
  state.transcript = tr;
  const done = tr.segments.filter((s) => s.verified).length;
  $('#wb-meta').textContent = `${fmtShort(state.duration)} · ${tr.segments.length} 句 · 已核对 ${done}/${tr.segments.length} · ${tr.model || ''}`;
  renderSegments();
  resizeWave();
  if (keep >= 0 && keep < tr.segments.length) selectSegment(keep);
  updateUndoButton();
}

/* ------------------------------------------------------------------ playback */
function whenReady(fn) {
  if (audio.readyState >= 1) fn();
  else audio.addEventListener('loadedmetadata', fn, { once: true });
}

function playSegment(i) {
  const s = state.transcript?.segments[i]; if (!s) return;
  state.selected = i; state.active = i; state.mode = 'segment';
  whenReady(() => {
    audio.currentTime = Math.max(0, s.start - 0.04);
    audio.play().catch(() => {});
  });
  $$('.seg').forEach((el) => el.classList.toggle('active', +el.dataset.i === i));
  $(`.seg[data-i="${i}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  paintWave();
}

function togglePlay() {
  if (!audio.paused) { audio.pause(); state.mode = 'idle'; return; }
  if (state.mode === 'segment' && state.active >= 0) {
    whenReady(() => { audio.currentTime = Math.max(0, state.transcript.segments[state.active].start - 0.04); });
  }
  state.mode = 'continuous';
  whenReady(() => audio.play().catch(() => {}));
}

function seek(t) {
  if (!state.duration) return;
  state.mode = 'continuous';
  whenReady(() => {
    audio.currentTime = Math.max(0, Math.min(state.duration - 0.05, t));
    audio.play().catch(() => {});
  });
}

function syncPlayIcon(playing) {
  const p = $('#p-play');
  if (p) p.textContent = playing ? '⏸' : '▶';
}
audio.addEventListener('play', () => syncPlayIcon(true));
audio.addEventListener('pause', () => { syncPlayIcon(false); setSelIcon(false); });

let ticking = false;
function startTick() { if (!ticking) { ticking = true; requestAnimationFrame(tick); } }
function tick() {
  const t = audio.currentTime || 0;
  if (state.duration) {
    const fill = $('#p-fill'); if (fill) fill.style.width = `${(t / state.duration) * 100}%`;
    const pt = $('#p-time'); if (pt) pt.textContent = `${fmtShort(t)} / ${fmtShort(state.duration)}`;
  }
  if (state.mode === 'range' && state.range) {
    const { a, b } = state.range;
    const segs = state.transcript.segments;
    const end = segs[b]?.end ?? 0;
    if (t >= end - 0.01) {
      if (state.selLoop && segs[a]) audio.currentTime = Math.max(0, segs[a].start - 0.02);
      else { audio.pause(); state.mode = 'idle'; setSelIcon(false); }
    } else {
      const i = findAt(t);
      if (i !== state.active && i >= a && i <= b) { state.active = i; highlightRow(i); }
    }
  } else if (state.mode === 'segment' && state.active >= 0) {
    const s = state.transcript.segments[state.active];
    if (t >= s.end - 0.01) {
      if (state.loop) audio.currentTime = Math.max(0, s.start - 0.02);
      else { audio.pause(); state.mode = 'idle'; }
    }
  } else if (state.mode === 'continuous') {
    const i = findAt(t);
    if (i !== state.active) { state.active = i; highlightRow(i); }
  }
  if (state.transcript) paintWave();
  requestAnimationFrame(tick);
}

function findAt(t) {
  const segs = state.transcript?.segments || [];
  let lo = 0, hi = segs.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (segs[mid].start <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

function highlightRow(i) {
  $$('.seg').forEach((el) => el.classList.toggle('active', +el.dataset.i === i));
  $(`.seg[data-i="${i}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

/* ------------------------------------------------------------------ waveform */
function resizeWave() {
  const c = $('#wave'); if (!c) return;
  const w = Math.max(300, c.clientWidth), h = 104;
  const dpr = window.devicePixelRatio || 1;
  c.width = w * dpr; c.height = h * dpr;
  c.style.height = `${h}px`;
  buildWaveBase(c, w, h, dpr);
  paintWave();
}

function buildWaveBase(c, w, h, dpr) {
  const base = document.createElement('canvas');
  base.width = w * dpr; base.height = h * dpr;
  const g = base.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const peaks = state.peaks || [];
  const n = peaks.length || 1;
  const bw = w / n;
  const cs = getComputedStyle(document.documentElement);
  const waveColor = cs.getPropertyValue('--wave').trim() || '#2b3a4a';
  for (let i = 0; i < n; i++) {
    const amp = Math.max(0.02, peaks[i] || 0);
    const bh = amp * (h - 18);
    g.fillStyle = waveColor;
    g.fillRect(i * bw, (h - bh) / 2, Math.max(0.7, bw * 0.8), bh);
  }
  if (state.transcript && state.duration) {
    g.strokeStyle = 'rgba(120,140,160,.25)';
    g.lineWidth = 1;
    for (const s of state.transcript.segments) {
      const x = (s.start / state.duration) * w;
      g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
    }
  }
  state.waveBase = { canvas: base, w, h, dpr };
}

function paintWave() {
  const c = $('#wave'); if (!c || !state.waveBase) return;
  const { canvas, w, h, dpr } = state.waveBase;
  const g = c.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  g.drawImage(canvas, 0, 0, w, h);
  if (state.selected >= 0 && state.transcript && state.duration) {
    const s = state.transcript.segments[state.selected];
    const x1 = (s.start / state.duration) * w, x2 = (s.end / state.duration) * w;
    g.fillStyle = 'rgba(74,168,255,.16)';
    g.fillRect(x1, 0, Math.max(2, x2 - x1), h);
    g.strokeStyle = 'rgba(74,168,255,.7)';
    g.strokeRect(x1 + .5, .5, Math.max(1, x2 - x1 - 1), h - 1);
  }
  if (state.duration) {
    const t = audio.currentTime || 0;
    const x = (t / state.duration) * w;
    g.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--playhead').trim() || '#e6edf3';
    g.fillRect(x, 0, 1.5, h);
    const left = $('#w-left'); if (left) left.textContent = fmtShort(t);
  }
}

/* ------------------------------------------------------------------ modals */
function askText(title, value) {
  return new Promise((resolve) => {
    const wrap = document.createElement('div');
    wrap.className = 'modal';
    wrap.innerHTML = `
      <div class="modal-box">
        <h3>${esc(title)}</h3>
        <input id="ask-input" value="${esc(value || '')}">
        <div class="modal-actions">
          <button class="primary" id="ask-ok">确定</button>
          <button id="ask-cancel">取消</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);
    const input = $('#ask-input', wrap);
    input.focus(); input.select();
    const done = (v) => { wrap.remove(); resolve(v); };
    $('#ask-ok', wrap).onclick = () => done(input.value.trim());
    $('#ask-cancel', wrap).onclick = () => done(null);
    input.onkeydown = (e) => {
      if (e.key === 'Enter') done(input.value.trim());
      if (e.key === 'Escape') done(null);
    };
    wrap.onclick = (e) => { if (e.target === wrap) done(null); };
  });
}

function askConfirm(title, text, okText = '确定') {
  return new Promise((resolve) => {
    const wrap = document.createElement('div');
    wrap.className = 'modal';
    wrap.innerHTML = `
      <div class="modal-box">
        <h3>${esc(title)}</h3>
        <p class="confirm-text">${esc(text)}</p>
        <div class="modal-actions">
          <button class="primary" id="cf-ok">${esc(okText)}</button>
          <button id="cf-cancel">取消</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);
    const done = (v) => {
      document.removeEventListener('keydown', onKey);
      wrap.remove();
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') done(false);
      if (e.key === 'Enter' && document.activeElement?.id !== 'cf-cancel') done(true);
    };
    document.addEventListener('keydown', onKey);
    $('#cf-ok', wrap).focus();
    $('#cf-ok', wrap).onclick = () => done(true);
    $('#cf-cancel', wrap).onclick = () => done(false);
    wrap.onclick = (e) => { if (e.target === wrap) done(false); };
  });
}

async function openStorage() {  let info;
  try { info = await api('/api/storage'); } catch (e) { toast(e.message); return; }
  const wrap = document.createElement('div');
  wrap.className = 'modal';
  wrap.innerHTML = `
    <div class="modal-box wide">
      <h3>数据位置</h3>
      <p class="muted">音频、转写数据库都在这里。修改后会把现有数据复制到新目录，重启应用生效。</p>
      <input id="st-path" value="${esc(info.path)}">
      <div class="modal-actions">
        <button id="st-open">打开目录</button>
        <button id="st-copy">复制路径</button>
        <button id="st-save">保存并迁移</button>
        <button id="st-close">关闭</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  const close = () => wrap.remove();
  $('#st-close', wrap).onclick = close;
  $('#st-copy', wrap).onclick = async () => {
    const ok = await copyText($('#st-path', wrap).value.trim());
    toast(ok ? '路径已复制' : '复制失败，请手动选中路径复制');
  };
  $('#st-open', wrap).onclick = async () => {
    try {
      await api('/api/open-folder', { method: 'POST' });
    } catch (e) {
      const ok = await copyText($('#st-path', wrap).value.trim());
      toast(ok ? '系统拒绝打开资源管理器，路径已复制，请手动粘贴打开。' : e.message, 6000);
    }
  };
  $('#st-save', wrap).onclick = async () => {
    const path = $('#st-path', wrap).value.trim();
    if (!path) return;
    if (!await askConfirm('迁移数据目录',
      '将把现有音频和数据库复制到新目录，并写入配置。重启应用后生效。', '开始迁移')) return;
    try {
      const r = await api('/api/storage', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      close();
      if (r.changed) toast(`已复制到 ${r.path}，请重启应用后生效。`, 6000);
      else toast('位置未变化，不需要重启。', 4000);
    } catch (e) { toast(e.message, 5000); }
  };
  wrap.onclick = (e) => { if (e.target === wrap) close(); };
}

/* ------------------------------------------------------------------- keyboard */
document.addEventListener('click', (e) => {
  if (state.editor < 0) return;
  if (e.target.closest('#editor, .seg, .modal, .toolbar, .player, .wb-head, .topbar')) return;
  closeEditorWithSave();
});

document.addEventListener('click', (e) => {
  if (e.target.closest('.undo-wrap')) return;
  const pop = $('#undo-pop');
  if (pop) pop.hidden = true;
});

document.addEventListener('keydown', (e) => {
  if (e.target.closest('textarea, input, select, button, a, [contenteditable="true"]')) return;
  if (window.getSelection()?.toString().trim()) return;
  if (!state.transcript) return;
  if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
  if (e.code === 'ArrowDown') { e.preventDefault(); playSegment(Math.min(state.transcript.segments.length - 1, (state.active < 0 ? 0 : state.active) + 1)); }
  if (e.code === 'ArrowUp') { e.preventDefault(); playSegment(Math.max(0, (state.active < 0 ? 0 : state.active) - 1)); }
  if (e.code === 'KeyL') { state.loop = !state.loop; const cb = $('#p-loop'); if (cb) cb.checked = state.loop; }
  if (e.code === 'KeyZ' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); undoLast(); }
  if (e.code === 'Escape') closeEditorWithSave();
});

document.getElementById('brand').onclick = async () => {
  await closeEditorWithSave();
  location.hash = '#/';
};
const THEME_KEY = 'listening-theme';
function applyTheme(v) {
  if (v === 'light' || v === 'dark') document.documentElement.dataset.theme = v;
  else delete document.documentElement.dataset.theme;
}
function initTheme() {
  const saved = localStorage.getItem(THEME_KEY) || 'auto';
  const sel = document.getElementById('theme');
  if (sel) {
    sel.value = saved;
    sel.onchange = () => {
      localStorage.setItem(THEME_KEY, sel.value);
      applyTheme(sel.value);
      if (state.transcript) { resizeWave(); paintWave(); }
    };
  }
  applyTheme(saved);
}

initTheme();
route();
