'use strict';
// hootl Web 管理画面。描画はすべて textContent 経由（ログやタスク本文を HTML として解釈しない）。

const STATIONS = [
  { key: 'todo', name: '待ち' },
  { key: 'setup', name: '準備' },
  { key: 'implement', name: '実装' },
  { key: 'guard', name: '安全柵' },
  { key: 'gate', name: 'ゲート' },
  { key: 'review', name: 'レビュー' },
  { key: 'merge', name: 'マージ' },
  { key: 'done', name: '完了' },
];
const STAGE_NAME = Object.fromEntries(STATIONS.map((s) => [s.key, s.name]));
const STATUS_NAME = { todo: '待ち', running: '実行中', done: '完了', blocked: '要対応' };
const PHASE_NAME = { draft: '仕様を書いています', freeze: '仕様を凍結しています', plan: 'タスクに分けています', import: '確かめて「待ち」に入れています', run: '回しています' };
const PHASES = ['draft', 'freeze', 'plan', 'import', 'run'];
const JOB_NAME = { run: 'ラン', plan: '計画', spec: '仕様の下書き', auto: 'おまかせ' };
const SOURCE_NAME = { human: '自分で', 'spec-writer': '仕様づくり中に', planner: '計画中に', implementer: '実装中に', reviewer: 'レビュー中に' };
const RESULT_STAGE = { agent: '実装', setup: '準備', guard: '安全柵', gate: 'ゲート', review: 'レビュー', merge: 'マージ', done: '完了' };

const state = { projects: [], current: null, data: null, tab: 'tasks', timer: null, armed: null,
  view: 'project', stacks: null, form: null, newTimer: null };

// ---- 小さな道具 ----
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (id) => document.getElementById(id);
const time = (iso) => (iso ? new Date(iso).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '');

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Hootl': '1' }, body: JSON.stringify(body),
  };
  const res = await fetch(path, { credentials: 'same-origin', ...opt });
  if (res.status === 401) { location.href = '/login'; throw new Error('ログインしてください'); }
  const ctype = res.headers.get('Content-Type') || '';
  const data = ctype.includes('json') ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.error) || `エラー（${res.status}）`);
  return data;
}

let toastTimer;
function toast(msg, bad) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast' + (bad ? ' bad' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 3000);
}
const P = () => `/api/p/${encodeURIComponent(state.current)}`;

// ---- 読み込み ----
async function loadProjects() {
  const { projects } = await api('/api/projects');
  state.projects = projects;
  const saved = (() => { try { return localStorage.getItem('hootl.project'); } catch { return null; } })();
  if (!state.current) state.current = projects.some((p) => p.name === saved) ? saved : projects[0]?.name;
  if (!state.current) state.view = 'new';   // まだ1つもないときは「新しいアプリ」から
  renderProjects();
}

async function refresh() {
  if (!state.current || state.view !== 'project') return;
  try {
    const wasAlive = state.data?.summary?.job?.alive;
    state.data = await api(`${P()}/tasks`);
    if (wasAlive && !state.data.summary.job?.alive) {
      if (state.tab === 'spec') renderSpec();
      if (state.tab === 'plan') renderPlan();
    }
    if (state.tab === 'ideas' && state.ideasSeen !== state.data.summary.ideas_new) renderIdeas();
    renderTabBadges();
    renderLine();
    renderRunBox();
    if (state.tab === 'tasks') renderTasks();
  } catch (e) {
    toast(e.message, true);
  }
  schedule();
}

function schedule() {
  clearTimeout(state.timer);
  const job = state.data?.summary?.job;
  const busy = (job && job.alive) || (state.data?.summary?.counts?.running || 0) > 0;
  state.timer = setTimeout(refresh, busy ? 4000 : 20000);
}

// ---- 描画: プロジェクト ----
function renderProjects() {
  const nav = $('projects');
  const chips = state.projects.map((p) => h('button', {
    type: 'button', 'aria-current': state.view === 'project' && p.name === state.current ? 'true' : 'false',
    onclick: () => openProject(p.name),
  }, p.name));
  chips.push(h('button', {
    type: 'button', class: 'new-app', 'aria-current': state.view === 'new' ? 'true' : 'false',
    onclick: () => openNew(),
  }, '＋ 新しいアプリ'));
  nav.replaceChildren(...chips);
}

function openProject(name, tab) {
  state.view = 'project';
  state.current = name;
  try { localStorage.setItem('hootl.project', name); } catch { /* 保存できなくても動く */ }
  $('new-view').hidden = true;
  $('main').hidden = false;
  clearTimeout(state.newTimer);
  renderProjects();
  switchTab(tab || state.tab);
  refresh();
}

// ---- 描画: ライン ----
function renderLine() {
  const tasks = state.data.tasks;
  const counts = state.data.summary.counts || {};
  const at = {};
  for (const t of tasks) if (t.status === 'running') (at[t.stage || 'setup'] ||= []).push(t.id);
  const line = $('line');
  line.replaceChildren(...STATIONS.map((s) => {
    let n, cls = 'station';
    if (s.key === 'todo') { n = counts.todo || 0; cls += n ? '' : ' idle'; }
    else if (s.key === 'done') { n = counts.done || 0; cls += ' done'; }
    else { n = (at[s.key] || []).length; cls += n ? ' busy' : ' idle'; }
    const who = s.key === 'todo' || s.key === 'done' ? '' : (at[s.key] || []).join(' ');
    return h('li', { class: cls },
      h('span', { class: 'node', 'aria-label': `${s.name} ${n}件` }, n || (s.key === 'done' ? 0 : '')),
      h('span', { class: 'name' }, s.name),
      h('span', { class: 'who' }, who));
  }));
  const blocked = counts.blocked || 0;
  const note = $('line-note');
  const err = state.data.summary.error;
  if (err) note.replaceChildren(h('span', { class: 'err' }, `読み込めません: ${err}`));
  else if (blocked) note.replaceChildren(h('strong', { style: 'color:var(--red)' }, `要対応 ${blocked}件`), '　ラインから外れたタスクがあります。「タスク」の一番上から確かめてください。');
  else if (!tasks.length) note.textContent = isAuto() ? 'まだタスクがありません。「仕様」に作りたいものを書くと、仕様・計画・実装まで進みます。' : 'まだタスクがありません。「計画と承認」でタスク案を作り、承認すると「待ち」に入ります。';
  else note.textContent = `全 ${tasks.length}件。止まっているタスクはありません。`;
}

// おまかせ（仕様と計画を人の承認なしで進める）が、このプロジェクトで有効か
const isAuto = () => !!state.data?.summary?.auto?.approve;

function renderTabBadges() {
  const n = state.data?.summary?.ideas_new || 0;
  const b = document.querySelector('.tabs button[data-tab="ideas"]');
  if (b) b.textContent = n ? `ネタ帳 ${n}` : 'ネタ帳';
  const waiting = state.data?.summary?.has_proposed;
  const pb = document.querySelector('.tabs button[data-tab="plan"]');
  if (pb) pb.textContent = waiting ? '計画と承認 ●' : '計画と承認';
}

// ---- 描画: 回す・止める ----
function renderRunBox() {
  const box = $('run-box');
  const job = state.data.summary.job;
  const counts = state.data.summary.counts || {};
  const kids = [];
  if (job && job.alive) {
    const started = new Date(job.started * 1000).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
    if (job.kind === 'run') {
      kids.push(h('div', { class: 'state' },
        h('strong', {}, job.stop_requested ? '止めています' : '回しています'),
        h('span', {}, job.stop_requested ? '今のタスクが終わったら止まります。すぐ止めるなら、もう一度押してください。' : `${started} から。新しいタスクを順に取っていきます。`)));
      kids.push(h('button', { class: 'btn stop', type: 'button', onclick: stopRun }, job.stop_requested ? 'すぐ止める' : '今のタスクで止める'));
    } else if (job.kind === 'auto') {
      const running = job.phase === 'run';
      const at = PHASES.indexOf(job.phase);
      kids.push(h('div', { class: 'state' },
        h('strong', {}, job.stop_requested ? '止めています' : `おまかせ: ${PHASE_NAME[job.phase] || '始めています'}`),
        h('span', {}, job.stop_requested && running ? '今のタスクが終わったら止まります。すぐ止めるなら、もう一度押してください。'
          : `${started} から。承認を待たずに、仕様 → 計画 → 実装まで進みます。`)));
      kids.push(h('ol', { class: 'phases', 'aria-label': 'おまかせの進み具合' }, PHASES.map((ph, i) => h('li', {
        class: i < at ? 'ok' : i === at ? 'now' : '', 'aria-current': i === at ? 'step' : null,
      }, { draft: '仕様', freeze: '凍結', plan: '計画', import: '取り込み', run: '実装' }[ph]))));
      kids.push(h('button', { class: 'btn stop', type: 'button', onclick: stopRun },
        running ? (job.stop_requested ? 'すぐ止める' : '今のタスクで止める') : '止める'));
    } else {
      kids.push(h('div', { class: 'state' }, h('strong', {}, job.kind === 'spec' ? '仕様の下書きを作っています' : 'タスク案を作っています'), h('span', {}, `${started} から。数分かかります。`)));
    }
  } else {
    const stopped = job && !job.alive && job.kind === 'auto' && job.code === 3;
    const last = !job || job.alive ? '' : stopped ? 'おまかせは、機械の確認で止まりました（出力の末尾に理由があります。続きは「仕様」か「計画と承認」から）。'
      : job.code == null ? `前回の${JOB_NAME[job.kind] || job.kind}は終わりました（管理画面の再起動をまたいだので、結果はタスクとレポートで確かめてください）。`
      : `前回の${JOB_NAME[job.kind] || job.kind}は${job.code === 0 ? '終わりました' : `失敗しました（コード ${job.code}・出力の末尾を見てください）`}。`;
    kids.push(h('div', { class: 'state' },
      h('strong', {}, '止まっています'),
      h('span', {}, `${last}待ち ${counts.todo || 0}件を回せます。`)));
    const hours = h('select', { id: 'run-hours', 'aria-label': '回す時間' },
      ...[['', '空になるまで'], ['1', '1時間'], ['2', '2時間'], ['4', '4時間'], ['7', '7時間'], ['10', '10時間']]
        .map(([v, l]) => h('option', { value: v, selected: v === '7' }, l)));
    const conc = h('select', { id: 'run-conc', 'aria-label': '並列' },
      ...[1, 2, 3, 4].map((n) => h('option', { value: n }, `${n}本`)));
    kids.push(h('label', {}, '時間', hours), h('label', {}, '並列', conc));
    kids.push(h('button', { class: 'btn go', type: 'button', disabled: !(counts.todo > 0), onclick: startRun }, '回す'));
  }
  if (job && job.tail) {
    kids.push(h('details', { style: 'width:100%', open: !job.alive && job.kind === 'auto' && job.code != null && job.code !== 0 }, h('summary', { style: 'cursor:pointer;color:var(--steel);font-size:13px' }, '出力の末尾'),
      h('pre', { class: 'job-tail' }, job.tail.split('\n').filter((l) => !l.startsWith('::phase ')).join('\n'))));
  }
  box.replaceChildren(...kids);
}

async function startRun() {
  try {
    await api(`${P()}/run`, { hours: $('run-hours').value || null, concurrency: Number($('run-conc').value) });
    toast('回し始めました');
  } catch (e) { toast(e.message, true); }
  refresh();
}
async function stopRun() {
  try { await api(`${P()}/stop`, {}); toast('止めます'); } catch (e) { toast(e.message, true); }
  refresh();
}

// ---- 描画: タスク ----
function row(t) {
  const meta = t.status === 'running'
    ? h('span', { class: 'meta' }, h('span', { class: 'stage' }, STAGE_NAME[t.stage] || '準備'), ` ・${t.attempts}回目`)
    : h('span', { class: 'meta' }, t.attempts ? `${t.attempts}回` : '', t.status === 'done' && t.updated_at ? ` ${time(t.updated_at)}` : '');
  return h('li', { class: 'row', tabindex: 0, role: 'button', onclick: () => openTask(t.id), onkeydown: (e) => { if (e.key === 'Enter') openTask(t.id); } },
    h('span', { class: 'id' }, t.id),
    h('span', { class: 'title' }, t.title, t.status === 'blocked' && t.last_error ? h('span', { class: 'why' }, t.last_error) : null),
    meta);
}

function renderTasks() {
  const tasks = state.data.tasks;
  const by = (s) => tasks.filter((t) => t.status === s);
  const blocked = by('blocked'), running = by('running'), todo = by('todo'), done = by('done').reverse();
  const group = (cls, title, list, empty) => h('section', { class: `group ${cls}` },
    h('h3', {}, title, h('span', { class: 'count' }, `${list.length}件`)),
    list.length ? h('ul', { class: 'rows' }, list.map(row)) : h('p', { class: 'empty' }, empty));
  const out = [];
  if (blocked.length) out.push(group('blocked', '要対応', blocked, ''));
  out.push(group('running', '実行中', running, '動いているタスクはありません。'));
  out.push(group('todo', '待ち', todo, '待ちはありません。'));
  out.push(h('details', { class: 'group' }, h('summary', {}, `完了 ${done.length}件`),
    done.length ? h('ul', { class: 'rows' }, done.map(row)) : h('p', { class: 'empty' }, 'まだありません。')));
  $('tab-tasks').replaceChildren(...out);
}

// ---- 詳細 ----
async function openTask(id) {
  let d;
  try { d = await api(`${P()}/task/${encodeURIComponent(id)}`); } catch (e) { return toast(e.message, true); }
  const t = d.task;
  const body = [];
  body.push(h('p', {}, h('span', { class: `pill ${t.status}` }, STATUS_NAME[t.status] || t.status), `　試行 ${t.attempts}回`, t.epic ? `　${t.epic}` : ''));
  if (t.status === 'blocked') {
    body.push(h('p', { class: 'err' }, t.last_error || ''));
    body.push(h('p', {}, h('button', { class: 'btn', type: 'button', onclick: () => resetTask(t.id) }, 'やり直す（待ちに戻す）')));
    body.push(h('p', { style: 'color:var(--steel);font-size:13px;margin-top:-4px' }, 'やり直す前に、下のログで原因を確かめ、必要ならタスク案を直して承認し直してください。'));
  }
  body.push(h('h3', {}, '中身'), h('p', { style: 'white-space:pre-wrap' }, t.description));
  body.push(h('h3', {}, '受け入れ条件'), h('ol', {}, t.acceptance.map((a) => h('li', {}, a))));
  body.push(h('h3', {}, '主に変更するパス'), h('ul', {}, t.paths.map((p) => h('li', {}, h('code', {}, p)))));
  if (t.depends_on.length) body.push(h('h3', {}, '先に終わっている必要があるタスク'), h('p', {}, t.depends_on.join('、')));
  body.push(h('h3', {}, '試行の記録'));
  const logBox = h('pre', { class: 'log', hidden: true });
  const fileBtns = (attempt, files) => h('div', { class: 'files' }, files.map((f) => h('button', {
    class: 'ghost', type: 'button',
    onclick: async () => {
      try {
        const text = await api(`${P()}/log?task=${encodeURIComponent(t.id)}&attempt=${attempt}&file=${encodeURIComponent(f)}`);
        logBox.textContent = text.trim() ? text : `${f} は空です（このコマンドは何も出力しませんでした）`;
        logBox.hidden = false;
        logBox.scrollIntoView({ block: 'nearest' });
      } catch (e) { toast(e.message, true); }
    },
  }, f)));
  if (!d.attempts.length && !d.in_progress.length) body.push(h('p', { class: 'empty' }, 'まだ試行していません。'));
  for (const a of d.attempts) {
    body.push(h('div', { class: `attempt ${a.ok ? 'ok' : 'ng'}` },
      h('div', {}, h('strong', {}, `${a.attempt}回目`), `　${a.ok ? '完了' : `${RESULT_STAGE[a.stage] || a.stage}で差し戻し`}`,
        h('span', { style: 'color:var(--steel);font-size:12.5px' }, `　${time(a.ended_at)}${a.turns ? `・${a.turns}ターン` : ''}`)),
      a.detail ? h('div', { style: 'font-size:13px;overflow-wrap:anywhere' }, a.detail) : null,
      fileBtns(a.attempt, a.files)));
  }
  for (const a of d.in_progress) {
    body.push(h('div', { class: 'attempt live' }, h('strong', {}, `${a.attempt}回目`), '　実行中', fileBtns(a.attempt, a.files)));
  }
  body.push(logBox);
  openSheet(`${t.id}　${t.title}`, body);
}

async function resetTask(id) {
  try {
    await api(`${P()}/reset`, { id });
    toast(`${id} を待ちに戻しました`);
    closeSheet();
    refresh();
  } catch (e) { toast(e.message, true); }
}

let lastFocus = null;
function openSheet(title, kids) {
  lastFocus = document.activeElement;
  $('sheet-title').textContent = title;
  $('sheet-body').replaceChildren(...kids);
  $('sheet').hidden = false;
  $('sheet-backdrop').hidden = false;
  $('sheet-close').focus();
}
function closeSheet() {
  $('sheet').hidden = true;
  $('sheet-backdrop').hidden = true;
  if (lastFocus) lastFocus.focus();
}

// ---- 計画と承認 ----
async function renderPlan() {
  const el = $('tab-plan');
  let d;
  try { d = await api(`${P()}/proposed`); } catch (e) { el.replaceChildren(h('p', { class: 'err' }, e.message)); return; }
  const job = state.data?.summary?.job;
  const planning = job && job.alive && job.kind === 'plan';
  const kids = [h('p', { class: 'plan-intro' }, isAuto()
    ? 'おまかせのときは、タスク案はここで止まらずに「待ち」へ入ります。ここに並ぶのは、機械の確認（id の重なり・行き先のない依存・件数）で止まった案と、自分で読みたくて作った案だけです。'
    : 'タスク案を読んで直し、承認します。承認したタスクだけが「待ち」に入ります（人が判断する唯一の場所）。新しく作りたいものは「仕様」タブから。')];
  kids.push(h('div', { class: 'plan-actions' },
    planning ? h('strong', {}, 'タスク案を作っています…（数分）') : null,
    !planning && d.spec_exists ? h('button', { class: 'ghost', type: 'button', onclick: startPlan }, `${d.spec} から作る`) : null,
    h('button', { class: 'ghost', type: 'button', onclick: () => switchTab('spec') }, '仕様タブへ')));
  if (planning || !d.text.trim()) {
    kids.push(h('p', { class: 'empty' }, planning ? 'できあがったら、ここに並びます。' : 'いまタスク案はありません。「仕様」タブで作りたいものを書くと、ここまで進みます。'));
    return el.replaceChildren(...kids);
  }
  if (d.error) kids.push(h('p', { class: 'err' }, `タスク案に問題があります: ${d.error}`));
  for (const t of d.tasks) {
    kids.push(h('article', { class: 'proposal' },
      h('h4', {}, `${t.id}　${t.title}`, t._existing ? h('span', { class: 'tag' }, `取り込み済み（${STATUS_NAME[t._existing] || t._existing}）・上書きされます`) : null),
      h('div', { class: 'sub' }, [t.epic, t.depends_on?.length ? `先に: ${t.depends_on.join('、')}` : null, `優先 ${t.priority ?? 100}`].filter(Boolean).join('　')),
      h('ol', {}, t.acceptance.map((a) => h('li', {}, a))),
      h('div', { class: 'sub' }, '変更するパス: ', t.paths.flatMap((p, i) => [i ? '、' : '', h('code', {}, p)]))));
  }
  const ta = h('textarea', { class: 'jsonl', spellcheck: 'false', 'aria-label': 'タスク案（JSONL）' }, d.text);
  const errLine = h('p', { class: 'err', hidden: true });
  const approve = h('button', { class: 'btn go', type: 'button', disabled: !!d.error, onclick: () => importPlan(approve) }, `承認して取り込む（${d.tasks.length}件）`);
  kids.push(h('details', {}, h('summary', { style: 'cursor:pointer;font-weight:600;margin:12px 0 8px' }, 'タスク案を直す（JSONL）'),
    ta, errLine,
    h('p', {}, h('button', {
      class: 'ghost', type: 'button',
      onclick: async () => {
        try { await api(`${P()}/proposed`, { text: ta.value }); toast('保存しました'); renderPlan(); }
        catch (e) { errLine.textContent = e.message; errLine.hidden = false; }
      },
    }, '保存する'))));
  const discard = h('button', { class: 'btn danger', type: 'button' }, 'この案を捨てる');
  discard.onclick = () => twoStep(discard, 'この案を捨てる', 'もう一度押すと捨てます', async () => {
    try { await api(`${P()}/proposed-discard`, {}); toast('タスク案を捨てました'); await refresh(); renderPlan(); } catch (e) { toast(e.message, true); }
  });
  kids.push(h('div', { class: 'plan-actions', style: 'margin-top:14px' }, approve, discard));
  el.replaceChildren(...kids);
}

async function startPlan() {
  try { await api(`${P()}/plan`, {}); toast('タスク案を作り始めました'); } catch (e) { toast(e.message, true); }
  await refresh();
  renderPlan();
}

async function importPlan(btn) {
  if (state.armed !== btn) { // 2回押しで確定（ダイアログを出さない）
    state.armed = btn;
    btn.textContent = 'もう一度押すと取り込みます';
    setTimeout(() => { if (state.armed === btn) { state.armed = null; renderPlan(); } }, 4000);
    return;
  }
  state.armed = null;
  try {
    const r = await api(`${P()}/import`, {});
    toast(`${r.imported}件を「待ち」に入れました`);
    switchTab('tasks');
    refresh();
  } catch (e) { toast(e.message, true); renderPlan(); }
}

// ---- 仕様 ----
const dateOf = (sec) => new Date(sec * 1000).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });

function twoStep(btn, label, confirmLabel, action) { // 2回押しで確定（ダイアログを出さない）
  if (state.armed !== btn) {
    state.armed = btn;
    btn.textContent = confirmLabel;
    setTimeout(() => { if (state.armed === btn) { state.armed = null; btn.textContent = label; } }, 4000);
    return;
  }
  state.armed = null;
  action();
}

async function renderSpec(openSlug) {
  const el = $('tab-spec');
  let d;
  try { d = await api(`${P()}/specs`); } catch (e) { el.replaceChildren(h('p', { class: 'err' }, e.message)); return; }
  const { drafts, frozen, base } = d.specs;
  const job = d.summary.job;
  const busy = job && job.alive;
  const kids = [];

  // 作りたいもの
  const idea = h('textarea', { id: 'idea', 'aria-label': '新しく作りたいもの',
    placeholder: '例: 間違えた過去問だけを、もう一度解ける復習モードがほしい。正解したら外れて、3回続けて正解したら卒業。' });
  try { idea.value = localStorage.getItem(`hootl.idea.${state.current}`) || ''; } catch { /* 保存できなくても動く */ }
  idea.addEventListener('input', () => { try { localStorage.setItem(`hootl.idea.${state.current}`, idea.value); } catch { /* 同上 */ } });
  const drafting = busy && (job.kind === 'spec' || (job.kind === 'auto' && job.phase === 'draft'));
  const auto = !!d.summary.auto?.approve;
  const start = async (asAuto) => {
    try {
      await api(`${P()}/spec-draft`, { idea: idea.value, auto: asAuto });
      try { localStorage.removeItem(`hootl.idea.${state.current}`); } catch { /* 同上 */ }
      toast(asAuto ? 'おまかせで作り始めました（仕様 → 計画 → 実装）' : '仕様の下書きを作り始めました');
      await refresh(); renderSpec();
    } catch (e) { toast(e.message, true); }
  };
  kids.push(h('section', { class: 'idea' },
    h('h3', { class: 'section-title', style: 'margin-top:0' }, '新しく作りたいもの'),
    h('p', { class: 'plan-intro', style: 'margin:0' }, auto
      ? '思いついたことを数行書くだけでよい。「おまかせで作る」を押すと、エージェントが仕様を書き、凍結し、タスクに分け、実装まで進めます（承認は挟まない）。決めきれない点はエージェントが決めて、仕様の「自分で決めたこと」に残します。'
      : '思いついたことを数行書くだけでよい。エージェントがコードと今の仕様を読んで、仕様の下書きにする（数分）。下書きは直してから凍結すると、タスク案づくりまで進む。'),
    idea,
    h('div', { class: 'plan-actions', style: 'margin:0' },
      auto ? h('button', { class: 'btn go', type: 'button', disabled: busy, onclick: () => start(true) }, 'おまかせで作る') : null,
      h('button', { class: auto ? 'ghost' : 'btn', type: 'button', disabled: busy, onclick: () => start(false) },
        drafting && job.kind === 'spec' ? '下書きを作っています…' : auto ? '下書きだけ作る（自分で読む）' : '仕様の下書きを作る'),
      busy ? h('span', { class: 'sub', style: 'color:var(--steel);font-size:13px' }, `いま ${JOB_NAME[job.kind] || job.kind} が動いています。思いついたことは「ネタ帳」に入れておけます`) : null)));

  // 下書き
  kids.push(h('h3', { class: 'section-title' }, `下書き ${drafts.length}件`));
  if (!drafts.length) kids.push(h('p', { class: 'empty' }, drafting ? '作っています。できたらここに出ます。' : 'まだありません。'));
  for (const s of drafts) kids.push(await draftCard(s, s.slug === openSlug, busy, auto));

  // 凍結済み
  kids.push(h('h3', { class: 'section-title' }, `凍結した仕様 ${frozen.length}件`));
  if (!frozen.length) kids.push(h('p', { class: 'empty' }, 'まだありません。'));
  for (const s of frozen) kids.push(frozenCard(s, busy));
  if (base) {
    kids.push(h('h3', { class: 'section-title' }, '全体の仕様'));
    kids.push(frozenCard({ ...base, title: `${base.title}（spec.md・どの計画にも前提として渡す）` }, busy));
  }
  el.replaceChildren(...kids);
}

async function draftCard(s, open, busy, auto) {
  const card = h('article', { class: 'spec-card' },
    h('h4', {}, s.title), h('div', { class: 'sub' }, `下書き・${dateOf(s.mtime)}`));
  const body = h('div', { hidden: !open });
  const toggle = h('button', { class: 'ghost', type: 'button', onclick: async () => {
    if (!body.hidden) { body.hidden = true; toggle.textContent = '開いて直す'; return; }
    try {
      const { text } = await api(`${P()}/spec?ref=${encodeURIComponent(`draft:${s.slug}`)}`);
      const ta = h('textarea', { class: 'spec-edit', spellcheck: 'false', 'aria-label': '仕様の下書き' }, text);
      const freezeLabel = auto ? '凍結してタスク案まで（自分で承認する）' : '凍結してタスク案を作る';
      const freezeBtn = h('button', { class: auto ? 'ghost' : 'btn go', type: 'button', disabled: busy }, freezeLabel);
      freezeBtn.onclick = () => twoStep(freezeBtn, freezeLabel, 'もう一度押すと凍結します', async () => {
        try {
          await api(`${P()}/spec-save`, { slug: s.slug, text: ta.value });
          await api(`${P()}/spec-freeze`, { slug: s.slug, auto: false });
          toast('凍結しました。タスク案を作っています');
          await refresh(); switchTab('plan');
        } catch (e) { toast(e.message, true); freezeBtn.textContent = freezeLabel; }
      });
      const goBtn = auto ? h('button', { class: 'btn go', type: 'button', disabled: busy }, '凍結して、あとはおまかせ') : null;
      if (goBtn) goBtn.onclick = () => twoStep(goBtn, '凍結して、あとはおまかせ', 'もう一度押すと実装まで進みます', async () => {
        try {
          await api(`${P()}/spec-save`, { slug: s.slug, text: ta.value });
          await api(`${P()}/spec-freeze`, { slug: s.slug, auto: true });
          toast('凍結しました。計画 → 実装まで進めます');
          await refresh(); switchTab('tasks');
        } catch (e) { toast(e.message, true); goBtn.textContent = '凍結して、あとはおまかせ'; }
      });
      const discardBtn = h('button', { class: 'btn danger', type: 'button' }, '捨てる');
      discardBtn.onclick = () => twoStep(discardBtn, '捨てる', 'もう一度押すと捨てます', async () => {
        try { await api(`${P()}/spec-discard`, { slug: s.slug }); toast('下書きを捨てました'); renderSpec(); } catch (e) { toast(e.message, true); }
      });
      body.replaceChildren(ta, h('div', { class: 'acts' },
        h('button', { class: 'ghost', type: 'button', onclick: async () => {
          try { await api(`${P()}/spec-save`, { slug: s.slug, text: ta.value }); toast('保存しました'); } catch (e) { toast(e.message, true); }
        } }, '保存する'),
        goBtn, freezeBtn, discardBtn),
        h('p', { class: 'sub', style: 'margin:8px 0 0' }, '凍結すると specs/ に入り、main にコミットされます。「決めてほしいこと」が残っていたら、答えを書き込んでから凍結してください（残したまま進めると、実装するエージェントが決めます）。'));
      body.hidden = false; toggle.textContent = '閉じる';
    } catch (e) { toast(e.message, true); }
  } }, '開いて直す');
  card.append(h('div', { class: 'acts' }, toggle), body);
  if (open) toggle.click();
  return card;
}

function frozenCard(s, busy) {
  return h('article', { class: 'spec-card' },
    h('h4', {}, s.title), h('div', { class: 'sub' }, `${s.path}・${dateOf(s.mtime)}`),
    h('div', { class: 'acts' },
      h('button', { class: 'ghost', type: 'button', onclick: async () => {
        try { const { text } = await api(`${P()}/spec?ref=${encodeURIComponent(s.path)}`); openSheet(s.title, [h('div', { class: 'spec-text' }, text)]); }
        catch (e) { toast(e.message, true); }
      } }, '読む'),
      h('button', { class: 'ghost', type: 'button', disabled: busy, onclick: async () => {
        try { await api(`${P()}/plan`, { ref: s.path }); toast('タスク案を作り始めました'); await refresh(); switchTab('plan'); }
        catch (e) { toast(e.message, true); }
      } }, 'この仕様からタスク案を作る')));
}

// ---- ネタ帳 ----
async function renderIdeas() {
  const el = $('tab-ideas');
  let d;
  try { d = await api(`${P()}/ideas`); } catch (e) { el.replaceChildren(h('p', { class: 'err' }, e.message)); return; }
  state.ideasSeen = d.summary.ideas_new;
  const job = d.summary.job;
  const busy = job && job.alive;
  const auto = !!d.summary.auto?.approve;
  const fresh = d.ideas.filter((i) => i.status === 'new');
  const past = d.ideas.filter((i) => i.status !== 'new');
  const act = async (path, body, msg) => {
    try { await api(`${P()}/${path}`, body); if (msg) toast(msg); await refresh(); renderIdeas(); } catch (e) { toast(e.message, true); }
  };
  const title = h('input', { type: 'text', placeholder: '例: 復習の時間になったら通知する', 'aria-label': '案の名前', maxlength: 80 });
  const body = h('textarea', { placeholder: '中身（空でもよい）。何が良くなるか、どこで思いついたか。', 'aria-label': '案の中身' });
  const kids = [
    h('p', { class: 'plan-intro' }, 'エージェントが、仕様づくり・計画・実装・レビューの途中で気づいた「今回は作らないが、よさそうな案」がここにたまります。たまるだけで、勝手には作りません。作るものを選ぶのはここ。「これを作る」を押すと、'
      + (auto ? '仕様 → 計画 → 実装まで進みます。' : '仕様の下書きになります。')),
    h('h3', { class: 'section-title' }, `新しい案 ${fresh.length}件`),
  ];
  if (!fresh.length) kids.push(h('p', { class: 'empty' }, 'まだありません。ランが進むと増えます。自分のメモも下から足せます。'));
  for (const i of fresh) {
    const build = h('button', { class: 'btn go', type: 'button', disabled: busy }, 'これを作る');
    build.onclick = () => twoStep(build, 'これを作る', auto ? 'もう一度押すと実装まで進みます' : 'もう一度押すと下書きを作ります',
      () => act('idea-build', { id: i.id }, auto ? `「${i.title}」をおまかせで作り始めました` : `「${i.title}」の下書きを作り始めました`));
    kids.push(h('article', { class: 'spec-card idea-card' },
      h('h4', {}, i.title),
      h('div', { class: 'sub' }, `${SOURCE_NAME[i.source] || i.source}${i.task_id ? `（${i.task_id}）` : ''}・${time(i.created_at)}`),
      i.body ? h('p', { class: 'idea-body' }, i.body) : null,
      h('div', { class: 'acts' }, build,
        h('button', { class: 'ghost', type: 'button', onclick: () => act('idea-drop', { id: i.id }, '捨てました（下の「これまでの案」から戻せます）') }, '捨てる'))));
  }
  kids.push(h('section', { class: 'idea' },
    h('h3', { class: 'section-title' }, '自分で足す'),
    title, body,
    h('div', { class: 'plan-actions', style: 'margin:0' },
      h('button', { class: 'btn', type: 'button', onclick: async () => {
        try {
          await api(`${P()}/idea-add`, { title: title.value, body: body.value });
          toast('ネタ帳に足しました'); await refresh(); renderIdeas();
        } catch (e) { toast(e.message, true); }
      } }, 'ネタ帳に足す'))));
  if (past.length) {
    kids.push(h('details', { class: 'group' }, h('summary', {}, `これまでの案 ${past.length}件`),
      h('ul', { class: 'rows' }, past.map((i) => h('li', { class: 'row idea-row' },
        h('span', { class: 'id' }, i.status === 'adopted' ? '作る' : '捨てた'),
        h('span', { class: 'title' }, i.title, i.body ? h('span', { class: 'why' }, i.body) : null),
        i.status === 'dropped' ? h('button', { class: 'ghost', type: 'button', onclick: () => act('idea-restore', { id: i.id }, '戻しました') }, '戻す') : h('span', { class: 'meta' }, time(i.created_at)))))));
  }
  el.replaceChildren(...kids);
}

// ---- レポート ----
async function renderReports() {
  const el = $('tab-reports');
  try {
    const { reports } = await api(`${P()}/reports`);
    el.replaceChildren(...(reports.length ? reports.map((r, i) => h('details', { class: 'report', open: i === 0 },
      h('summary', {}, r.name.replace(/^report-|\.md$/g, '').replace('T', ' ')),
      h('pre', {}, r.text))) : [h('p', { class: 'empty' }, 'まだレポートはありません。ランが終わるたびに1つ増えます。')]));
  } catch (e) { el.replaceChildren(h('p', { class: 'err' }, e.message)); }
}

// ---- タブ ----
// ---- 新しいアプリ（カタログから）----
const MARK = { todo: '・', start: '…', ok: '✓', fail: '✕', skip: '－' };

async function openNew() {
  state.view = 'new';
  clearTimeout(state.timer);
  $('main').hidden = true;
  $('new-view').hidden = false;
  renderProjects();
  try {
    if (!state.stacks) state.stacks = await api('/api/stacks');
  } catch (e) { toast(e.message, true); return; }
  if (!state.form) {
    const first = state.stacks.stacks[0];
    state.form = { name: '', title: '', idea: '', auto: true, stack: first.name, options: [], gates: defaultGates(first, []) };
  }
  renderNew();
  pollCreations();
}

const stackOf = (name) => state.stacks.stacks.find((s) => s.name === name);
function gatesFor(stack, options) {
  return stack.gates.filter((g) => !g.option || options.includes(g.option));
}
function defaultGates(stack, options) {
  return gatesFor(stack, options).filter((g) => g.default || g.required).map((g) => g.name);
}

function renderNew() {
  const f = state.form;
  const stack = stackOf(f.stack);
  const view = $('new-view');
  const bind = (key) => (e) => { f[key] = e.target.value; if (key === 'name') updateWhere(); };

  const stackCards = h('div', { class: 'stack-grid', role: 'radiogroup', 'aria-label': '型' },
    state.stacks.stacks.map((s) => h('button', {
      type: 'button', class: 'stack-card', role: 'radio', 'aria-checked': String(s.name === f.stack),
      onclick: () => { f.stack = s.name; f.options = []; f.gates = defaultGates(s, []); renderNew(); },
    }, h('strong', {}, s.title), h('span', { class: 'tech' }, s.tech), h('span', { class: 'what' }, s.summary))));

  const optionList = stack.options.length ? h('ul', { class: 'checks' }, stack.options.map((o) => {
    const blocked = o.conflicts.some((c) => f.options.includes(c));
    const box = h('input', { type: 'checkbox', checked: f.options.includes(o.id), disabled: blocked && !f.options.includes(o.id) });
    box.addEventListener('change', () => {
      f.options = box.checked ? [...f.options, o.id] : f.options.filter((x) => x !== o.id);
      // オプションが持ち込むゲートは、付けたら使う・外したら消す
      const own = stack.gates.filter((g) => g.option === o.id).map((g) => g.name);
      f.gates = box.checked ? [...new Set([...f.gates, ...own.filter((n) => stack.gates.find((g) => g.name === n).default)])]
        : f.gates.filter((n) => !own.includes(n));
      renderNew();
    });
    const conflictNote = o.conflicts.length ? `（${o.conflicts.map((c) => stack.options.find((x) => x.id === c)?.title || c).join('、')} とは一緒に使えない）` : '';
    return h('li', {}, h('label', { class: blocked ? 'off' : null }, box, h('span', { class: 't' }, o.title),
      h('span', { class: 'd' }, o.summary + conflictNote)));
  })) : h('p', { class: 'empty' }, 'この型にはオプションがありません。');

  const gateList = h('ul', { class: 'checks' }, gatesFor(stack, f.options).map((g) => {
    const box = h('input', { type: 'checkbox', checked: g.required || f.gates.includes(g.name), disabled: g.required });
    box.addEventListener('change', () => {
      f.gates = box.checked ? [...f.gates, g.name] : f.gates.filter((x) => x !== g.name);
    });
    return h('li', {}, h('label', { class: g.required ? 'locked' : null }, box,
      h('span', { class: 't' }, g.title + (g.required ? '（必須）' : '')),
      h('span', { class: 'd' }, h('code', {}, g.cmd))));
  }));

  const where = h('span', { class: 'where', id: 'new-where' });
  const go = h('button', { class: 'btn go', type: 'button', onclick: createApp }, '作る');
  const autoBox = h('input', { type: 'checkbox', checked: f.auto !== false });
  autoBox.addEventListener('change', () => { f.auto = autoBox.checked; });
  view.replaceChildren(
    h('h2', {}, '新しいアプリ'),
    h('p', { class: 'lead' }, '型を選ぶと、フォルダを切って git を始め、依存を入れ、全部のゲートと起動確認が通るところまで進めます（数分）。作りたいものを書いておくと、仕様を書き、そのまま計画 → 実装まで進めます（承認は挟まない）。'),
    h('label', { class: 'field' }, h('span', {}, '作りたいもの ', h('small', {}, '数行でよい。空でも作れる（あとで「仕様」タブから）')),
      Object.assign(h('textarea', { placeholder: '例: 家計簿。レシートの写真から金額を読んで、月ごとに費目別の合計を出したい。', oninput: bind('idea') }), { value: f.idea })),
    h('div', { class: 'two' },
      h('label', { class: 'field' }, h('span', {}, '名前 ', h('small', {}, 'フォルダ名・英小文字とハイフン')),
        Object.assign(h('input', { type: 'text', placeholder: 'kakeibo', autocomplete: 'off', spellcheck: 'false', oninput: bind('name') }), { value: f.name })),
      h('label', { class: 'field' }, h('span', {}, '表示名 ', h('small', {}, '画面やタイトルに出る名前')),
        Object.assign(h('input', { type: 'text', placeholder: '家計簿', oninput: bind('title') }), { value: f.title }))),
    h('div', { class: 'field' }, h('span', {}, '型'), stackCards),
    h('div', { class: 'field' }, h('span', {}, 'オプション'), optionList),
    h('div', { class: 'field' }, h('span', {}, 'ゲート ', h('small', {}, '全部通ったものだけが main に入る。あとから「設定」タブで切り替えられる')), gateList),
    h('ul', { class: 'checks' }, h('li', {}, h('label', {}, autoBox, h('span', { class: 't' }, 'できたら、そのまま実装まで進める（おまかせ）'),
      h('span', { class: 'd' }, '作りたいものを書いたときだけ。外すと、仕様の下書きで止まり、自分で読んでから進められる')))),
    h('div', { class: 'new-actions' }, go, where),
    h('h3', { class: 'section-title' }, '作っているもの・作ったもの'),
    h('div', { id: 'creations' }, h('p', { class: 'empty' }, 'まだありません。')),
  );
  updateWhere();
  renderCreations();
}

function updateWhere() {
  const el = $('new-where');
  if (!el) return;
  const dir = state.stacks.runs_dir;
  el.textContent = dir ? `${dir}/${state.form.name || '（名前）'} に作ります` : 'web.toml に projects_dir がないので作れません（hootl setup を実行してください）';
}

async function createApp() {
  const f = state.form;
  try {
    await api('/api/new', { name: f.name.trim(), title: f.title, idea: f.idea, stack: f.stack, options: f.options, gates: f.gates, auto: f.auto !== false });
    toast(`${f.name} を作り始めました`);
    state.form = { ...f, name: '', title: '', idea: '' };
    renderNew();
    await pollCreations();
    $('creations')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) { toast(e.message, true); }
}

async function pollCreations() {
  clearTimeout(state.newTimer);
  if (state.view !== 'new') return;
  let alive = false;
  try {
    const before = new Set((state.creations || []).filter((c) => c.result === 'done').map((c) => c.name));
    state.creations = (await api('/api/new')).creations;
    alive = state.creations.some((c) => c.alive);
    if (state.creations.some((c) => c.result === 'done' && !before.has(c.name))) await loadProjects();
    renderCreations();
  } catch (e) { toast(e.message, true); }
  state.newTimer = setTimeout(pollCreations, alive ? 2000 : 15000);
}

function renderCreations() {
  const box = $('creations');
  if (!box || !state.creations) return;
  if (!state.creations.length) { box.replaceChildren(h('p', { class: 'empty' }, 'まだありません。')); return; }
  box.replaceChildren(...state.creations.map((c) => {
    const status = c.alive ? '作っています' : c.result === 'done' ? 'できました' : '途中で止まりました';
    const acts = [];
    if (c.result === 'done') {
      acts.push(h('button', { class: 'btn go', type: 'button', onclick: async () => { await loadProjects(); openProject(c.name, c.note ? 'tasks' : 'spec'); } }, '開く'));
      if (c.note) acts.push(h('span', { class: 'sub' }, c.note));
    }
    if (c.result === 'failed') {
      acts.push(h('button', { class: 'btn', type: 'button', onclick: () => retryCreation(c) }, '同じ内容で作り直す'));
    }
    if (!c.alive) {
      acts.push(h('button', { class: 'ghost', type: 'button', onclick: () => dismissCreation(c.name) }, '一覧から消す'));
    }
    if (c.result === 'failed' && c.moved_to) acts.push(h('span', { class: 'sub' }, `途中までのものは ${c.moved_to} に退避しました（消していません）`));
    const failed = c.steps.find((s) => s.state === 'fail');
    return h('article', { class: 'creation' },
      h('h3', {}, c.name, h('span', { class: 'sub' }, `${status}・${new Date(c.started * 1000).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' })} から`)),
      h('ol', { class: 'steps' }, c.steps.map((s) => h('li', { class: s.state },
        h('span', { class: 'mark', 'aria-hidden': 'true' }, MARK[s.state] || '・'),
        h('span', {}, s.label, s.note ? h('span', { class: 'note' }, s.note) : null)))),
      acts.length ? h('div', { class: 'acts' }, acts) : null,
      (c.alive || failed) && c.tail ? h('details', { open: !!failed },
        h('summary', {}, '出力の末尾'), h('pre', { class: 'log' }, c.tail.split('\n').filter((l) => !l.startsWith('::')).join('\n').trim() || '（出力はありません）')) : null);
  }));
}

function retryCreation(c) {
  const r = c.request || { name: c.name };
  const stack = stackOf(r.stack) || stackOf(state.form.stack);
  const options = (r.options || []).filter((o) => stack.options.some((x) => x.id === o));
  state.form = {
    name: r.name || c.name, title: r.title || '', idea: r.idea || '', auto: r.auto !== false, stack: stack.name, options,
    gates: Array.isArray(r.gates) ? r.gates : defaultGates(stack, options),
  };
  renderNew();
  window.scrollTo({ top: 0, behavior: 'smooth' });
  toast('前の内容を入れました。型とゲートを確かめて「作る」を押してください');
}

async function dismissCreation(name) {
  try {
    await api('/api/new/dismiss', { name });
    await pollCreations();
  } catch (e) { toast(e.message, true); }
}

// ---- 設定（ゲートの切り替え）----
async function renderSettings() {
  const el = $('tab-settings');
  let d;
  try { d = await api(`${P()}/settings`); } catch (e) { el.replaceChildren(h('p', { class: 'err' }, e.message)); return; }
  const kids = [];
  const cat = d.catalog;
  kids.push(h('dl', { class: 'settings-meta' },
    h('dt', {}, '場所'), h('dd', {}, h('code', {}, d.repo)),
    cat ? [h('dt', {}, '型'), h('dd', {}, `${cat.stack.title}（${cat.stack.tech}）`),
      h('dt', {}, 'オプション'), h('dd', {}, cat.options.map((o) => o.title).join('、') || 'なし'),
      cat.ports.length ? [h('dt', {}, 'ポート'), h('dd', {}, cat.ports.map((x) => `${x.label} ${x.port}`).join('・'))] : null] : null,
    h('dt', {}, 'レビュー役'), h('dd', {}, h('code', {}, d.reviewer || 'なし')),
    h('dt', {}, 'おまかせ'), h('dd', {}, d.auto.approve
      ? `仕様と計画を承認なしで進める・${d.auto.run ? `取り込んだらそのまま回す（${d.auto.hours ? `${d.auto.hours}時間まで` : '空になるまで'}）` : '回すのは自分で'}・1つの仕様から ${d.auto.max_tasks} 件を超えたら人に回す`
      : '使わない（下書きとタスク案で止まり、自分で承認する）',
      h('div', { class: 'sub', style: 'color:var(--steel);font-size:12.5px' }, '変えるときは hootl.toml の [auto]（approve / run / hours / max_tasks）'))));

  if (!cat) {
    kids.push(h('h3', { class: 'section-title' }, 'ゲート'),
      h('p', { class: 'plan-intro' }, 'このプロジェクトはカタログから作っていないので、ゲートは hootl.toml の [[gates]] を直接直します。'),
      h('ul', { class: 'checks' }, d.gates.map((g) => h('li', {}, h('label', { class: 'locked' },
        h('input', { type: 'checkbox', checked: true, disabled: true }), h('span', { class: 't' }, g.name),
        h('span', { class: 'd' }, h('code', {}, g.cmd)))))));
    el.replaceChildren(...kids);
    return;
  }
  const chosen = new Set(cat.gates.filter((g) => g.on).map((g) => g.name));
  const save = h('button', { class: 'btn', type: 'button', disabled: true }, '保存して main にコミット');
  const list = h('ul', { class: 'checks' }, cat.gates.map((g) => {
    const box = h('input', { type: 'checkbox', checked: g.on, disabled: g.required });
    box.addEventListener('change', () => {
      if (box.checked) chosen.add(g.name); else chosen.delete(g.name);
      const same = cat.gates.every((x) => x.on === chosen.has(x.name));
      save.disabled = same;
    });
    return h('li', {}, h('label', { class: g.required ? 'locked' : null }, box,
      h('span', { class: 't' }, g.title + (g.required ? '（必須）' : '')),
      h('span', { class: 'd' }, h('code', {}, g.cmd))));
  }));
  save.onclick = async () => {
    try {
      await api(`${P()}/gates`, { gates: [...chosen] });
      toast('ゲートを切り替えて、main にコミットしました');
      renderSettings();
    } catch (e) { toast(e.message, true); }
  };
  kids.push(h('h3', { class: 'section-title' }, 'ゲート'),
    h('p', { class: 'plan-intro' }, '全部通ったものだけが main に入ります。中身は型ごとに共通（hootl-sdd の stacks/）で、ここでは使う・使わないを選びます。次のタスクから効きます。'),
    list, h('div', { class: 'plan-actions', style: 'margin-top:12px' }, save));
  if (cat.custom.length) {
    kids.push(h('h3', { class: 'section-title' }, 'このアプリだけのゲート'),
      h('p', { class: 'plan-intro' }, 'hootl.toml の [[gates]] に書いたもの。カタログのゲートの後に走ります。'),
      h('ul', { class: 'checks' }, cat.custom.map((g) => h('li', {}, h('label', { class: 'locked' },
        h('input', { type: 'checkbox', checked: true, disabled: true }), h('span', { class: 't' }, g.name),
        h('span', { class: 'd' }, h('code', {}, g.cmd)))))));
  }
  el.replaceChildren(...kids);
}

function switchTab(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll('.tabs button')) b.setAttribute('aria-selected', String(b.dataset.tab === tab));
  for (const name of ['tasks', 'spec', 'ideas', 'plan', 'reports', 'settings']) $(`tab-${name}`).hidden = name !== tab;
  if (tab === 'tasks' && state.data) renderTasks();
  if (tab === 'spec') renderSpec();
  if (tab === 'ideas') renderIdeas();
  if (tab === 'plan') renderPlan();
  if (tab === 'reports') renderReports();
  if (tab === 'settings') renderSettings();
}

document.querySelector('.tabs').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-tab]');
  if (b) switchTab(b.dataset.tab);
});
$('sheet-close').addEventListener('click', closeSheet);
$('sheet-backdrop').addEventListener('click', closeSheet);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('sheet').hidden) closeSheet(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });

loadProjects().then(() => (state.view === 'new' ? openNew() : refresh())).catch((e) => toast(e.message, true));
