const msg = document.getElementById('msg');
const view = document.getElementById('view');
const gateBadge = document.getElementById('gate-badge');
let contracts = [];
let lastCheck = null;

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || j.plain || ('HTTP ' + r.status));
  return j;
}

function setMsg(t, isErr = false) {
  msg.textContent = t || '';
  msg.className = isErr ? 'err' : 'muted';
}

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function chip(text, kind = '') {
  return `<span class="chip ${kind}">${esc(text)}</span>`;
}

function statusChip(status) {
  const kind = status === '已入库' ? 'ok' : status === '拒收' ? 'bad' : status === '待放行' ? 'wait' : 'warn';
  return chip(status, kind);
}

async function loadContracts() {
  const data = await api('/api/contracts');
  contracts = data.items || [];
}

async function loadGate() {
  const data = await api('/api/gate');
  const g = data.gate;
  gateBadge.textContent = `闸口凭证：${g.mechanism} · ${g.credentials.length} 张齐契凭证`;
  return g;
}

function renderContracts() {
  view.innerHTML = `<div class="grid">${contracts.map((c) => `
    <article class="card">
      <div class="row">
        <h3>${esc(c.table_name)} · v${esc(c.version_no)}</h3>
        <span class="chip">${esc(c.status)}</span>
      </div>
      <p class="muted">${esc(c.note || '')} · 字段 ${esc(c.field_count)}</p>
    </article>`).join('')}</div>`;
}

function draftOptions() {
  return contracts.filter((c) => c.status === '草稿')
    .map((c) => `<option value="${esc(c.id)}">${esc(c.table_name)} v${esc(c.version_no)}</option>`).join('');
}

function renderCheck() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-check" type="button">运行破坏性检查</button>
      <pre id="out" class="muted"></pre>
    </div>`;
  document.getElementById('btn-check').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/breaking-check`, { method: 'POST', body: '{}' });
      lastCheck = j.check;
      document.getElementById('out').textContent = JSON.stringify(j.check, null, 2);
      setMsg(`检查结论：${j.check.verdict}`);
    } catch (e) { setMsg(e.message, true); }
  };
}

function renderApply() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-apply" type="button">换版应用</button>
      <p class="muted small">换版与齐契凭证同事务原子切换：应用成功后，入站立即按新凭证校验。</p>
    </div>`;
  document.getElementById('btn-apply').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/apply`, { method: 'POST', body: '{}' });
      setMsg(`已应用 ${j.contract.table_name} v${j.contract.version_no}`);
      await loadContracts();
      await loadGate();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function renderGate() {
  const g = await loadGate();
  const events = (await api('/api/gate-events')).items || [];
  view.innerHTML = `
    <div class="grid">
      <article class="card">
        <div class="row"><h3>工程凭证（只择一）</h3>${chip(g.mechanism, 'ok')}</div>
        <p class="muted">${esc(g.rationale)}</p>
        <p class="muted small">切换于 ${esc(g.switched_at || '—')}</p>
      </article>
      <article class="card">
        <h3>旁路齐契凭证表</h3>
        <div class="grid">${g.credentials.map((c) => `
          <div class="line">
            <strong>${esc(c.table_name)}</strong>
            ${chip(`v${c.version_no}`)}
            <span class="muted small">指纹 ${esc(c.fingerprint)}</span>
            <span class="muted small">字段：${esc(Object.entries(c.fields).map(([k, v]) => `${k} ${v}`).join(' / '))}</span>
          </div>`).join('') || '<p class="muted">暂无齐契凭证</p>'}
        </div>
      </article>
      <article class="card grid">
        <h3>页上核闸口</h3>
        <div class="row">
          <input id="gate-table" placeholder="表名，如 dw_orders_d" />
          <button class="primary" id="btn-gate-check" type="button">核闸口</button>
        </div>
        <pre id="gate-out" class="muted small"></pre>
      </article>
      <article class="card">
        <h3>编排台账（入档可查）</h3>
        <table class="rows">
          <thead><tr><th>时间</th><th>批次</th><th>表</th><th>动作</th><th>结果</th><th>说明</th><th>值班</th></tr></thead>
          <tbody>${events.map((e) => `
            <tr>
              <td class="mono">${esc(e.created_at)}</td>
              <td class="mono">${esc(e.batch_id ?? '—')}</td>
              <td>${esc(e.table_name || '—')}</td>
              <td>${esc(e.action)}</td>
              <td>${chip(e.outcome, /拒/.test(e.outcome) ? 'bad' : 'ok')}</td>
              <td>${esc(e.message)}</td>
              <td>${esc(e.operator)}</td>
            </tr>`).join('') || '<tr><td colspan="7" class="muted">暂无台账</td></tr>'}</tbody>
        </table>
      </article>
    </div>`;
  document.getElementById('btn-gate-check').onclick = async () => {
    try {
      const table_name = document.getElementById('gate-table').value.trim();
      const j = await api('/api/gate/check', { method: 'POST', body: JSON.stringify({ table_name }) });
      document.getElementById('gate-out').textContent = JSON.stringify(j.check, null, 2);
      setMsg(`闸口结论：${j.check.detail}`);
    } catch (e) { setMsg(e.message, true); }
  };
}

function fateChip(b) {
  const kind = b.status === '已入库' ? 'ok' : b.status === '拒收' ? 'bad' : b.status === '已回滚' ? 'warn' : 'wait';
  return chip(`命运签·${b.fate}`, kind);
}

async function renderInbox() {
  const items = (await api('/api/ingest-batches')).items || [];
  view.innerHTML = `
    <div class="grid">
      <article class="card grid">
        <h3>送一票入站报文</h3>
        <div class="row">
          <input id="in-table" placeholder="表名" />
          <input id="in-rows" class="grow" value='[{"order_id": 11, "amount": "9.90", "biz_date": "2026-11-19"}]' />
          <button class="primary" id="btn-ingest" type="button">入站</button>
        </div>
        <p class="muted small">齐契才进待放行；缺契、未知列当场拒收，绝不落库。</p>
      </article>
      <article class="card">
        <div class="row"><h3>入站列表</h3><span class="muted small">共 ${items.length} 票</span></div>
        <div class="grid">${items.map((b) => `
          <div class="line">
            <span class="mono">${esc(b.batch_no)}</span>
            <strong>${esc(b.table_name)}</strong>
            ${statusChip(b.status)}
            ${chip(b.verdict, b.verdict === '齐契' ? 'ok' : 'bad')}
            ${fateChip(b)}
            <span class="muted small">${esc(b.detail)}</span>
            <span class="spacer"></span>
            ${b.status === '待放行' ? `<button type="button" class="primary" data-release="${esc(b.id)}">点放行</button>` : ''}
            ${b.status === '已入库' ? `<button type="button" class="ghost" data-rollback="${esc(b.id)}">整笔回滚</button>` : ''}
          </div>`).join('') || '<p class="muted">还没有入站报文</p>'}
        </div>
      </article>
    </div>`;
  document.getElementById('btn-ingest').onclick = async () => {
    let table_name = '';
    let rows = [];
    try {
      table_name = document.getElementById('in-table').value.trim();
      try { rows = JSON.parse(document.getElementById('in-rows').value || '[]'); }
      catch { throw new Error('报文不是合法 JSON'); }
      if (!Array.isArray(rows)) throw new Error('报文必须是行数组');
      const j = await api('/api/ingest', { method: 'POST', body: JSON.stringify({ table_name, rows }) });
      setMsg(j.message);
    } catch (e) {
      setMsg(e.message, true);
    }
    // 拒收也刷新，让这票立刻进拒收卡片与命运签。
    await renderInbox();
  };
  view.querySelectorAll('[data-release]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const j = await api(`/api/ingest-batches/${btn.dataset.release}/release`, { method: 'POST', body: '{}' });
        setMsg(j.message);
        await renderInbox();
      } catch (e) { setMsg(e.message, true); await renderInbox(); }
    };
  });
  view.querySelectorAll('[data-rollback]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const j = await api(`/api/ingest-batches/${btn.dataset.rollback}/rollback`, { method: 'POST', body: '{}' });
        setMsg(j.message);
        await renderInbox();
      } catch (e) { setMsg(e.message, true); }
    };
  });
}

async function renderRejects() {
  const all = (await api('/api/ingest-batches')).items || [];
  const rejects = all.filter((b) => b.status === '拒收');
  view.innerHTML = `
    <div class="grid">
      ${rejects.map((b) => `
        <article class="card reject">
          <div class="row">
            <h3>${esc(b.table_name)}</h3>
            ${chip(b.verdict, 'bad')}
            ${fateChip(b)}
            <span class="muted small mono">${esc(b.batch_no)}</span>
          </div>
          <p>${esc(b.detail)}</p>
          ${b.rejected_rows.length ? `<table class="rows">
            <thead><tr><th>行号</th><th>原因</th><th>缺契/越界列</th></tr></thead>
            <tbody>${b.rejected_rows.map((r) => `
              <tr class="${r.reason === '缺契' ? 'missing' : ''}">
                <td>${esc(r.row_no)}</td>
                <td>${esc(r.reason)}</td>
                <td class="mono">${esc(r.columns.length ? r.columns.join(', ') : '—')}</td>
              </tr>`).join('')}</tbody>
          </table>` : '<p class="muted small">整票拒收，无逐行明细</p>'}
          <p class="muted small">落库行数：0（拒收即不落库）</p>
        </article>`).join('') || '<p class="muted">暂无拒收</p>'}
    </div>`;
}

async function renderReconcile() {
  const reports = (await api('/api/reconcile-reports')).items || [];
  const batches = (await api('/api/ingest-batches')).items || [];
  view.innerHTML = `
    <div class="grid">
      <article class="card grid">
        <div class="row">
          <input id="as-of" value="2026-11-19" />
          <button class="primary" id="btn-rec" type="button">跑一轮对账</button>
        </div>
        <p class="muted small">对账会复验入站行：缺契已入库行整笔清掉，复验结论里不会再见到。</p>
      </article>
      <article class="card">
        <h3>命运签</h3>
        <div class="grid">${batches.map((b) => `
          <div class="line">
            <span class="mono">${esc(b.batch_no)}</span>
            <strong>${esc(b.table_name)}</strong>
            ${fateChip(b)}
            <span class="muted small">${esc(b.detail)}</span>
          </div>`).join('') || '<p class="muted">暂无入站</p>'}
        </div>
      </article>
      <article class="card">
        <h3>对账进度</h3>
        <div class="grid">${reports.map((r) => `
          <div class="line">
            <strong>${esc(r.as_of)}</strong>
            ${chip(`对齐 ${r.matched}`, 'ok')}
            ${chip(`漂移 ${r.drifted}`, r.drifted ? 'bad' : '')}
            ${r.orphan_catalog ? chip(`目录孤儿 ${r.orphan_catalog}`, 'warn') : ''}
            ${r.detail && r.detail.missing_credentials && r.detail.missing_credentials.length
              ? chip(`契约缺契 ${r.detail.missing_credentials.length}`, 'bad') : ''}
            ${r.purged_missing_rows ? chip(`缺契清理 ${r.purged_missing_rows}`, 'warn') : ''}
            ${r.detail && r.detail.legacy_batches && r.detail.legacy_batches.length
              ? chip(`换版前入库 ${r.detail.legacy_batches.length}`, 'wait') : ''}
            ${chip(`缺契入库 ${r.contract_missing_rows}`, r.contract_missing_rows ? 'bad' : 'ok')}
            <span class="muted small">${esc(r.summary)}</span>
          </div>`).join('') || '<p class="muted">暂无对账</p>'}
        </div>
      </article>
    </div>`;
  document.getElementById('btn-rec').onclick = async () => {
    try {
      const as_of = document.getElementById('as-of').value.trim() || '2026-11-19';
      const j = await api('/api/reconcile-reports', { method: 'POST', body: JSON.stringify({ as_of }) });
      setMsg(j.report.summary, j.report.contract_missing_rows > 0);
      await renderReconcile();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function show(tab) {
  setMsg('');
  await loadContracts();
  await loadGate().catch(() => {});
  if (tab === 'contracts') renderContracts();
  if (tab === 'check') renderCheck();
  if (tab === 'apply') renderApply();
  if (tab === 'gate') await renderGate();
  if (tab === 'inbox') await renderInbox();
  if (tab === 'rejects') await renderRejects();
  if (tab === 'reconcile') await renderReconcile();
}

document.querySelectorAll('.pills button').forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll('.pills button').forEach((b) => b.classList.remove('on'));
    btn.classList.add('on');
    show(btn.dataset.tab);
  };
});
document.getElementById('btn-logout').onclick = async () => {
  await api('/api/logout', { method: 'POST', body: '{}' });
  location.href = '/login';
};
show('contracts');
