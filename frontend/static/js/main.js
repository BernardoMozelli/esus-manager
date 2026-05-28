// ── Estado ──────────────────────────────────────────────────────────────────
let ambientes = [];
let versoes = [];
let versoesFilt = [];
let ambienteSel = null;
let versaoSel = null;
let sessionId = null;
let etapaAtual = 0;
let editandoId = null;
let executando = false;

const ETAPAS = [
  {
    n: 1,
    titulo: "Verificar versão no site do e-SUS",
    ajuda: "O sistema irá acessar o site oficial do Ministério da Saúde e confirmar que o arquivo da versão escolhida está disponível para download.",
  },
  {
    n: 2,
    titulo: "Parar serviço no servidor de aplicação",
    ajuda: "O serviço do e-SUS PEC será interrompido para garantir consistência durante o backup e a atualização. O sistema ficará indisponível temporariamente.",
  },
  {
    n: 3,
    titulo: "Realizar backup do banco de dados",
    ajuda: "Uma cópia de segurança do banco será feita antes de qualquer alteração. Em caso de erro, o banco pode ser restaurado a partir deste backup.",
  },
  {
    n: 4,
    titulo: "Baixar o instalador no servidor",
    ajuda: "O arquivo .jar da nova versão será baixado diretamente no servidor de aplicação a partir do site do Ministério da Saúde.",
  },
  {
    n: 5,
    titulo: "Aplicar atualização",
    ajuda: "O instalador será executado. Ao final, o e-SUS PEC será reiniciado automaticamente na nova versão.",
  },
];

// ── Navegação ──────────────────────────────────────────────────────────────
function ir(telaId, btn) {
  document.querySelectorAll(".tela").forEach((t) => t.classList.remove("ativa"));
  document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
  document.getElementById(telaId).classList.add("ativa");
  btn.classList.add("active");
  if (telaId === "tela-ambientes") carregarAmbientes();
  if (telaId === "tela-atualizar") renderAmbSelectList();
  if (telaId === "tela-versoes") carregarTabelaVersoes();
}

// ── Tela de Versões ────────────────────────────────────────────────────────
async function carregarTabelaVersoes() {
  const tbody = document.getElementById("versoes-tbody");
  const msg = document.getElementById("versoes-sync-msg");
  const count = document.getElementById("versoes-sync-count");

  tbody.innerHTML = `<tr><td colspan="4" style="padding:2rem;text-align:center;color:#94a3b8">Carregando...</td></tr>`;

  const [rv, rs] = await Promise.all([
    fetch("/api/versoes").then((r) => r.json()),
    fetch("/api/versoes/status").then((r) => r.json()),
  ]);

  if (rs.rodando) {
    msg.innerHTML = `<svg class="spin" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Buscando novas versões...`;
    msg.style.color = "#0052A3";
    // Polling com limite de 20 tentativas (~80s)
    const tentativasEl = document.getElementById("versoes-sync-tentativas") || (() => {
      const el = document.createElement("span");
      el.id = "versoes-sync-tentativas";
      el.dataset.n = "0";
      el.style.display = "none";
      document.body.appendChild(el);
      return el;
    })();
    const n = parseInt(tentativasEl.dataset.n || "0");
    if (n < 20) {
      tentativasEl.dataset.n = n + 1;
      setTimeout(carregarTabelaVersoes, 4000);
    } else {
      tentativasEl.dataset.n = "0";
      msg.innerHTML = "⚠ Sincronização demorou demais. Tente novamente.";
      msg.style.color = "#f59e0b";
    }
  } else if (rs.erro) {
    msg.innerHTML = `⚠ ${esc(rs.erro)}`;
    msg.style.color = "#dc2626";
    if (rs.debug_ultimo?.length) {
      const debugEl = document.getElementById("versoes-debug");
      if (debugEl) debugEl.remove();
      const div = document.createElement("div");
      div.id = "versoes-debug";
      div.style.cssText = "margin-top:.5rem;padding:.6rem .9rem;background:#fff7ed;border:1px solid #fed7aa;border-radius:6px;font-family:monospace;font-size:.72rem;color:#92400e;line-height:1.7";
      div.innerHTML = "<strong>Debug do script remoto:</strong><br>" + rs.debug_ultimo.map((l) => esc(l)).join("<br>");
      msg.parentNode.insertBefore(div, msg.nextSibling);
    }
  } else if (rs.ultima_sync) {
    const debugEl = document.getElementById("versoes-debug");
    if (debugEl) debugEl.remove();
    msg.innerHTML = `✓ Última sincronização: ${rs.ultima_sync}`;
    msg.style.color = "#15803d";
    if (rs.debug_ultimo?.length) {
      const div = document.createElement("div");
      div.id = "versoes-debug";
      div.style.cssText = "margin-top:.4rem;padding:.5rem .9rem;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;font-family:monospace;font-size:.71rem;color:#166534;line-height:1.7";
      div.innerHTML = '<details><summary style="cursor:pointer;font-weight:600">Ver log da descoberta</summary><br>' + rs.debug_ultimo.map((l) => esc(l)).join("<br>") + "</details>";
      msg.parentNode.insertBefore(div, msg.nextSibling);
    }
  } else {
    msg.innerHTML = `Nenhuma sincronização realizada ainda.`;
    msg.style.color = "#94a3b8";
  }
  count.textContent = rv.length ? `${rv.length} versão(ões)` : "";

  if (!rv.length) {
    tbody.innerHTML = `<tr><td colspan="4" style="padding:2rem;text-align:center;color:#94a3b8">
      Nenhuma versão encontrada. Clique em <strong>Buscar novas versões</strong>.
    </td></tr>`;
    return;
  }

  tbody.innerHTML = rv.map((v) => `
    <tr style="border-bottom:1px solid #f1f5f9" id="row-${v.versao}">
      <td style="padding:.7rem 1.2rem;font-weight:700;white-space:nowrap">
        ${v.versao}
        ${v.versao === rv[0]?.versao && v.tem_url ? '<span style="background:#dcfce7;color:#166534;font-size:.68rem;padding:.1rem .45rem;border-radius:4px;font-weight:700;margin-left:.4rem">MAIS RECENTE</span>' : ""}
      </td>
      <td style="padding:.7rem 1.2rem;max-width:380px">
        ${v.tem_url
          ? `<span style="font-family:monospace;font-size:.75rem;color:#0052A3;word-break:break-all">${esc(v.url_linux || "")}</span>`
          : `<span style="color:#94a3b8;font-style:italic">Não descoberta ainda</span>`}
      </td>
      <td style="padding:.7rem 1.2rem;text-align:center" id="status-${v.versao}">
        ${v.tem_url
          ? `<span style="background:#f0fdf4;color:#15803d;padding:.2rem .6rem;border-radius:5px;font-size:.75rem;font-weight:700">✓ URL OK</span>`
          : `<span style="background:#f8fafc;color:#94a3b8;padding:.2rem .6rem;border-radius:5px;font-size:.75rem">— sem URL</span>`}
      </td>
      <td style="padding:.7rem 1.2rem;text-align:center;white-space:nowrap">
        <div style="display:flex;gap:.4rem;justify-content:center">
          ${v.tem_url
            ? `<button class="btn btn-ghost btn-sm" onclick="testarUrl('${v.versao}')">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
            Testar
          </button>`
            : ""}
          <button class="btn btn-danger btn-sm" onclick="redescobrir('${v.versao}')" title="Remover e redescobrir URL">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
            Redescobrir
          </button>
        </div>
      </td>
    </tr>`).join("");
}

async function testarUrl(versao) {
  const cell = document.getElementById(`status-${versao}`);
  cell.innerHTML = `<span style="color:#94a3b8;font-size:.75rem"><svg class="spin" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Testando...</span>`;
  const r = await fetch("/api/versoes/testar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ versao }),
  });
  const d = await r.json();
  if (d.ok) {
    cell.innerHTML = `<span style="background:#dcfce7;color:#15803d;padding:.2rem .6rem;border-radius:5px;font-size:.75rem;font-weight:700">✓ Acessível (${d.status})</span>`;
  } else {
    cell.innerHTML = `<span style="background:#fee2e2;color:#dc2626;padding:.2rem .6rem;border-radius:5px;font-size:.75rem" title="${esc(d.erro || "")}">✗ Inacessível (${d.status || "?"})</span>`;
  }
}

async function redescobrir(versao) {
  const res = await Swal.fire({
    title: `Redescobrir versão ${versao}?`,
    text: "A URL atual será removida e o sistema buscará novamente na próxima sincronização.",
    icon: "warning",
    showCancelButton: true,
    confirmButtonText: "Sim, redescobrir",
    cancelButtonText: "Cancelar",
    confirmButtonColor: "#f59e0b",
  });
  if (!res.isConfirmed) return;
  await fetch(`/api/versoes/${versao}`, { method: "DELETE" });
  await carregarTabelaVersoes();
}

async function sincronizarVersoesTabela() {
  await fetch("/api/versoes/sync", { method: "POST" });
  await new Promise((r) => setTimeout(r, 500));
  await carregarTabelaVersoes();
}

// ── Stepper ────────────────────────────────────────────────────────────────
function initStepper() {
  document.getElementById("stepper").innerHTML = ETAPAS.map((e) => `
    <div class="step" id="step-${e.n}">
      <div class="step-dot">${e.n}</div>
      <div class="step-content">
        <div class="step-titulo">${e.titulo}</div>
        <div class="step-sub" id="step-sub-${e.n}">Aguardando...</div>
        <div class="step-help">${e.ajuda}</div>
        <div class="confirm-box" id="confirm-${e.n}">
          <div class="confirm-titulo">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            Confirmar execução desta etapa
          </div>
          <div class="confirm-desc">Verifique as informações no log abaixo e clique em <strong>Confirmar</strong> para executar, ou <strong>Cancelar</strong> para interromper o processo.</div>
          <div class="confirm-btns">
            <button class="btn btn-success btn-sm" onclick="confirmarEtapa()">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
              Confirmar e Executar
            </button>
            <button class="btn btn-danger btn-sm" onclick="cancelarEtapa()">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              Cancelar Processo
            </button>
          </div>
        </div>
      </div>
    </div>
  `).join("");
}

function setStep(n, status, sub) {
  const el = document.getElementById(`step-${n}`);
  if (!el) return;
  el.className = `step s-${status}`;
  if (sub) document.getElementById(`step-sub-${n}`).textContent = sub;
}
function showConfirm(n, show) {
  const b = document.getElementById(`confirm-${n}`);
  if (b) b.className = `confirm-box${show ? " show" : ""}`;
}

// ── Log ────────────────────────────────────────────────────────────────────
function addLog(ts, msg, tipo) {
  const body = document.getElementById("log-body");
  const l = document.createElement("div");
  l.className = `log-line log-${tipo}`;
  l.innerHTML = `<span class="log-ts">${ts || ""}</span><span class="log-msg">${esc(msg)}</span>`;
  body.appendChild(l);
  body.scrollTop = body.scrollHeight;
}
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function limparLog() {
  document.getElementById("log-body").innerHTML = "";
}
function setStatus(msg, cls) {
  const dot = cls === "s-run" ? '<span class="dot"></span>' : "";
  document.getElementById("status-bar").innerHTML = `<span class="${cls}">${dot}${msg}</span>`;
}

// ── Ambientes ──────────────────────────────────────────────────────────────
async function carregarAmbientes() {
  const res = await fetch("/api/ambientes");
  ambientes = await res.json();
  renderAmbLista();
  renderAmbSelectList();
}

function tipoTag(tipo) {
  const m = {
    homologacao: '<span class="tag tag-h">HOMOLOG</span>',
    producao: '<span class="tag tag-p">PROD</span>',
  };
  return m[tipo] || '<span class="tag tag-o">OUTRO</span>';
}

function renderAmbLista() {
  const el = document.getElementById("amb-lista");
  if (!ambientes.length) {
    el.innerHTML = `<div class="empty-state">
      <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
      <p>Nenhum ambiente cadastrado ainda.<br>Clique em <strong>+ Novo Ambiente</strong> para adicionar um servidor.</p>
    </div>`;
    return;
  }
  el.innerHTML = ambientes.map((a) => `
    <div class="amb-item">
      <div class="ai-tipo">${tipoTag(a.tipo)}</div>
      <div class="ai-info">
        <div class="ai-nome">${esc(a.nome)}</div>
        <div class="ai-hosts">App: ${a.app_host || "—"} &nbsp;|&nbsp; BD: ${a.bd_host || "—"} (${a.db_name || "—"})</div>
      </div>
      <div class="ai-actions">
        <button class="btn btn-ghost btn-sm" onclick="editarAmbiente(${a.id})">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          Editar
        </button>
        <button class="btn btn-danger btn-sm" onclick="deletarAmbiente(${a.id},'${esc(a.nome)}')">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
        </button>
      </div>
    </div>`).join("");
}

function renderAmbSelectList() {
  const el = document.getElementById("amb-select-list");
  if (!ambientes.length) {
    el.innerHTML = `<div class="empty-amb">Nenhum ambiente cadastrado.<br><a href="#" onclick="ir('tela-ambientes',document.getElementById('nav-ambientes'))">Clique aqui para cadastrar</a></div>`;
    return;
  }
  el.innerHTML = ambientes.map((a) => `
    <div class="amb-opt ${ambienteSel === a.id ? "sel" : ""}" onclick="selecionarAmbiente(${a.id})">
      <svg class="ao-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
      <div class="ao-info">
        <div class="ao-nome">${tipoTag(a.tipo)} ${esc(a.nome)}</div>
        <div class="ao-host">${a.app_host || ""} | BD: ${a.bd_host || ""}</div>
      </div>
      <svg class="ao-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
    </div>`).join("");
}

function selecionarAmbiente(id) {
  ambienteSel = id;
  renderAmbSelectList();
  verificarPronto();
}

// ── Versões ────────────────────────────────────────────────────────────────
async function carregarVersoes() {
  const el = document.getElementById("versao-list");
  el.innerHTML = `<div class="versao-loading"><svg class="spin" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Carregando versões...</div>`;
  try {
    const [rv, rs] = await Promise.all([
      fetch("/api/versoes").then((r) => r.json()),
      fetch("/api/versoes/status").then((r) => r.json()),
    ]);
    versoes = rv;
    versoesFilt = [...versoes];
    // Só entra em polling se estiver de fato rodando
    if (rs.rodando) {
      atualizarStatusSync(rs);
    } else {
      atualizarStatusSync(rs, 99); // skip polling, só exibe status
    }
    renderVersoes();
  } catch (e) {
    el.innerHTML = `<div class="versao-erro">Erro ao carregar versões.</div>`;
  }
}

function atualizarStatusSync(st, _tentativas = 0) {
  const el = document.getElementById("sync-status");
  if (!el) return;
  if (st.rodando && _tentativas < 20) {
    el.innerHTML = `<svg class="spin" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Buscando novas versões no site do e-SUS...`;
    el.style.color = "#0052A3";
    setTimeout(async () => {
      const s = await fetch("/api/versoes/status").then((r) => r.json());
      if (!s.rodando) {
        const rv = await fetch("/api/versoes").then((r) => r.json());
        versoes = rv;
        versoesFilt = [...versoes];
        renderVersoes();
      }
      atualizarStatusSync(s, _tentativas + 1);
    }, 3000);
    return;
  }
  if (st.rodando && _tentativas >= 20) {
    el.innerHTML = `⚠ Sincronização demorou demais. Tente novamente.`;
    el.style.color = "#f59e0b";
    return;
  }
  if (st.erro) {
    el.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#dc2626" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg> Erro: ${esc(st.erro)}`;
    el.style.color = "#dc2626";
  } else if (st.ultima_sync) {
    el.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#15803d" stroke-width="2" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg> Última sync: ${st.ultima_sync}${st.novas ? " · " + st.novas + " nova(s)" : ""}`;
    el.style.color = "#15803d";
  } else {
    el.innerHTML = `Clique em <strong>Atualizar</strong> para buscar novas versões no site do e-SUS.`;
    el.style.color = "#94a3b8";
  }
}

async function sincronizarVersoes() {
  const btn = document.getElementById("btn-sync");
  btn.disabled = true;
  await fetch("/api/versoes/sync", { method: "POST" });
  await new Promise((r) => setTimeout(r, 500));
  const st = await fetch("/api/versoes/status").then((r) => r.json());
  atualizarStatusSync(st);
  btn.disabled = false;
}

function onFiltroVersao() {
  const q = document.getElementById("versao-filtro").value.trim();
  versoesFilt = q ? versoes.filter((v) => v.versao.includes(q)) : [...versoes];
  renderVersoes();
}

function renderVersoes() {
  const el = document.getElementById("versao-list");
  if (!versoes.length) {
    el.innerHTML = `<div class="versao-loading" style="color:#f59e0b"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg> Nenhuma versão. Clique em <strong>Atualizar</strong>.</div>`;
    return;
  }
  if (!versoesFilt.length) {
    el.innerHTML = `<div class="versao-erro">Nenhuma versão para este filtro.</div>`;
    return;
  }
  el.innerHTML = versoesFilt.map((v, i) => `
    <div class="versao-item ${versaoSel && versaoSel.versao === v.versao ? "sel" : ""}" onclick="selecionarVersao(${versoes.indexOf(v)})">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="${v.tem_url ? "#7BC043" : "#94a3b8"}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      <div style="flex:1">
        <div class="vi-num">Versão ${v.versao}
          ${i === 0 && v.tem_url ? '<span style="background:#dcfce7;color:#166534;font-size:.68rem;padding:.1rem .45rem;border-radius:4px;font-weight:700;margin-left:.3rem">MAIS RECENTE</span>' : ""}
          ${!v.tem_url ? '<span style="background:#f1f5f9;color:#94a3b8;font-size:.68rem;padding:.1rem .45rem;border-radius:4px;margin-left:.3rem">sem URL</span>' : ""}
        </div>
        <div class="vi-file">${v.arquivo}</div>
      </div>
      <svg class="vi-check" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#7BC043" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
    </div>`).join("");
}

function selecionarVersao(idx) {
  versaoSel = versoes[idx];
  renderVersoes();
  verificarPronto();
}

// ── Validação ──────────────────────────────────────────────────────────────
function verificarPronto() {
  const ok = ambienteSel && versaoSel && versaoSel.tem_url && !executando;
  const btn = document.getElementById("btn-iniciar");
  if (btn) btn.disabled = !ok;
}

// ── Iniciar ────────────────────────────────────────────────────────────────
async function iniciarAtualizacao() {
  if (!ambienteSel || !versaoSel || executando) return;

  const usarModoAuto = document.getElementById("modo_auto").checked;

  executando = true;
  verificarPronto();
  etapaAtual = 0;
  limparLog();
  initStepper();
  setStatus("Iniciando sessão...", "s-run");

  const payload = {
    ambiente_id: ambienteSel,
    versao: versaoSel.versao,
    url_download: versaoSel.url_linux,
    modo_auto: usarModoAuto,
  };

  const r = await fetch("/api/iniciar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const d = await r.json();
  if (!d.sid && !d.session_id) {
    Swal.fire({ title: "Erro ao iniciar", text: "Não foi possível iniciar a sessão.", icon: "error" });
    executando = false;
    verificarPronto();
    return;
  }

  sessionId = d.sid || d.session_id;
  document.getElementById("exec-status").textContent = `#${sessionId.substring(0, 6)}`;
  ouvirStream();
}

// ── SSE ────────────────────────────────────────────────────────────────────
function ouvirStream() {
  const es = new EventSource(`/api/stream/${sessionId}`);
  es.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if (!d.msg || d.tipo === "ping") return;

    if (d.tipo === "controle") {
      if (d.msg === "__AGUARDA__") {
        showConfirm(etapaAtual, true);
        setStep(etapaAtual, "active", "Aguardando sua confirmação...");
        setStatus(`Etapa ${etapaAtual}: aguardando confirmação`, "s-run");
      } else if (d.msg === "__FIM_OK__") {
        setStatus("✓ Atualização concluída com sucesso!", "s-ok");
        executando = false;
        verificarPronto();
        document.getElementById("exec-status").textContent = "";
        es.close();
      } else if (d.msg === "__FIM_ERRO__") {
        if (etapaAtual) setStep(etapaAtual, "s-error", "Erro — veja o log.");
        setStatus("Atualização encerrou com erro.", "s-err");
        showConfirm(etapaAtual, false);
        executando = false;
        verificarPronto();
        es.close();
      }
      return;
    }

    if (d.tipo === "etapa") {
      const parts = d.msg.split("__");
      const n = parseInt(parts[2]);
      const titulo = parts[3] || "";
      if (etapaAtual && etapaAtual !== n) setStep(etapaAtual, "s-done", "Concluído ✓");
      etapaAtual = n;
      setStep(n, "active", "Preparando...");
      showConfirm(n, false);
      addLog(d.ts, `━━ ETAPA ${n}: ${titulo} ━━`, "etapa");
      return;
    }

    addLog(d.ts, d.msg, d.tipo);
  };
  es.onerror = () => {
    setStatus("Conexão perdida com o servidor.", "s-err");
    es.close();
    executando = false;
    verificarPronto();
  };
}

async function confirmarEtapa() {
  showConfirm(etapaAtual, false);
  setStep(etapaAtual, "active", "Executando...");
  setStatus(`Executando etapa ${etapaAtual}...`, "s-run");
  await fetch(`/api/confirmar/${sessionId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
}

async function cancelarEtapa() {
  showConfirm(etapaAtual, false);
  setStep(etapaAtual, "s-error", "Cancelado.");
  setStatus("Processo cancelado pelo usuário.", "s-err");
  await fetch(`/api/confirmar/${sessionId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cancelar: true }),
  });
  executando = false;
  verificarPronto();
}

// ── Modal ──────────────────────────────────────────────────────────────────
function abrirModal(id = null) {
  editandoId = id;
  const t = document.getElementById("modal-titulo");
  t.innerHTML = id
    ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0052A3" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>Editar Ambiente`
    : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0052A3" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>Novo Ambiente`;
  if (!id) limparModal();
  document.getElementById("modal-overlay").classList.add("open");
}

function fecharModal() {
  document.getElementById("modal-overlay").classList.remove("open");
  editandoId = null;
}

function limparModal() {
  ["f-nome", "f-app-host", "f-app-usuario", "f-app-senha", "f-bd-host", "f-bd-usuario", "f-bd-senha", "f-bd-nome"]
    .forEach((id) => (document.getElementById(id).value = ""));
  document.getElementById("f-tipo").value = "homologacao";
  document.getElementById("f-app-porta").value = "22";
  document.getElementById("f-app-dir").value = "/root/e-SUS";
  document.getElementById("f-app-service").value = "e-SUS-PEC";
  document.getElementById("f-app-docker").checked = false;
  document.getElementById("f-bd-porta-ssh").value = "22";
  document.getElementById("f-bd-container").value = "postgresql-db-1";
}

async function editarAmbiente(id) {
  const r = await fetch(`/api/ambientes/${id}`);
  const d = await r.json();
  document.getElementById("f-nome").value = d.nome;
  document.getElementById("f-tipo").value = d.tipo;
  document.getElementById("f-app-host").value = d.srv_app?.host || "";
  document.getElementById("f-app-porta").value = d.srv_app?.porta || 22;
  document.getElementById("f-app-usuario").value = d.srv_app?.usuario || "";
  document.getElementById("f-app-senha").value = d.srv_app?.senha || "";
  document.getElementById("f-app-dir").value = d.srv_app?.esus_dir || "/root/e-SUS";
  document.getElementById("f-app-service").value = d.srv_app?.service || "e-SUS-PEC";
  document.getElementById("f-app-docker").checked = !!d.srv_app?.usa_docker;
  document.getElementById("f-bd-host").value = d.srv_bd?.host || "";
  document.getElementById("f-bd-porta-ssh").value = d.srv_bd?.porta || 22;
  document.getElementById("f-bd-usuario").value = d.srv_bd?.usuario || "";
  document.getElementById("f-bd-senha").value = d.srv_bd?.senha || "";
  document.getElementById("f-bd-nome").value = d.srv_bd?.db_name || "";
  document.getElementById("f-bd-container").value = d.srv_bd?.container || "postgresql-db-1";
  abrirModal(id);
}

async function salvarAmbiente() {
  const nome = document.getElementById("f-nome").value.trim();
  if (!nome) {
    Swal.fire({ title: "Campo obrigatório", text: "Informe o nome do ambiente.", icon: "warning" });
    return;
  }
  if (!document.getElementById("f-app-host").value.trim() || !document.getElementById("f-bd-host").value.trim()) {
    Swal.fire({ title: "Campos obrigatórios", text: "Preencha os endereços dos servidores de aplicação e banco.", icon: "warning" });
    return;
  }
  const p = {
    nome,
    tipo: document.getElementById("f-tipo").value,
    app_host: document.getElementById("f-app-host").value.trim(),
    app_porta: parseInt(document.getElementById("f-app-porta").value) || 22,
    app_usuario: document.getElementById("f-app-usuario").value.trim(),
    app_senha: document.getElementById("f-app-senha").value,
    app_dir: document.getElementById("f-app-dir").value.trim() || "/root/e-SUS",
    app_service: document.getElementById("f-app-service").value.trim() || "e-SUS-PEC",
    app_docker: document.getElementById("f-app-docker").checked,
    bd_host: document.getElementById("f-bd-host").value.trim(),
    bd_porta: parseInt(document.getElementById("f-bd-porta-ssh").value) || 22,
    bd_usuario: document.getElementById("f-bd-usuario").value.trim(),
    bd_senha: document.getElementById("f-bd-senha").value,
    bd_nome: document.getElementById("f-bd-nome").value.trim(),
    bd_container: document.getElementById("f-bd-container").value.trim() || "postgresql-db-1",
  };
  const r = await fetch(editandoId ? `/api/ambientes/${editandoId}` : "/api/ambientes", {
    method: editandoId ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(p),
  });
  const d = await r.json();
  if (d.ok || d.id) {
    fecharModal();
    await carregarAmbientes();
    Swal.fire({ title: "Salvo!", text: "Ambiente salvo com sucesso.", icon: "success", timer: 1500, showConfirmButton: false });
  } else {
    Swal.fire({ title: "Erro ao salvar", text: JSON.stringify(d), icon: "error" });
  }
}

async function deletarAmbiente(id, nome) {
  const res = await Swal.fire({
    title: `Remover "${nome}"?`,
    text: "Esta ação não pode ser desfeita.",
    icon: "warning",
    showCancelButton: true,
    confirmButtonText: "Sim, remover",
    cancelButtonText: "Cancelar",
    confirmButtonColor: "#dc2626",
  });
  if (!res.isConfirmed) return;
  await fetch(`/api/ambientes/${id}`, { method: "DELETE" });
  if (ambienteSel === id) { ambienteSel = null; verificarPronto(); }
  await carregarAmbientes();
}

// ── Toggle senha ───────────────────────────────────────────────────────────
function toggleSenha(inputId, btn) {
  const inp = document.getElementById(inputId);
  const visible = inp.type === "text";
  inp.type = visible ? "password" : "text";
  btn.innerHTML = visible
    ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`
    : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
}

// ── Init ───────────────────────────────────────────────────────────────────
initStepper();
carregarAmbientes();
carregarVersoes();
