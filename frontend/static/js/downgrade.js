// ── Downgrade
// Acessa getAmbientes() do escopo global (definido em main.js)
function getAmbientes() { return window.ambientes || []; }
let dgAmbienteSel = null;
let dgVersaoSel   = null;
let dgVersoes     = [];
let dgVersoesFilt = [];
let dgExecutando  = false;
let dgEtapaAtual  = 0;
let dgSessionId   = null;

const DG_ETAPAS = [
  { n: 1, titulo: "Parar serviço no servidor",        ajuda: "O serviço do e-SUS PEC será interrompido. O sistema ficará indisponível durante o downgrade." },
  { n: 2, titulo: "Backup de segurança do banco",     ajuda: "Um dump completo do banco será feito antes de qualquer alteração. Use para reverter em caso de problema." },
  { n: 3, titulo: "Executar desinstalador nativo",    ajuda: "O desinstalador remove binários e limpa os registros do Java que impedem instalar versões anteriores." },
  { n: 4, titulo: "Download do instalador da versão", ajuda: "O arquivo .jar da versão de destino será baixado diretamente no servidor." },
  { n: 5, titulo: "Instalar versão de destino",       ajuda: "O instalador será executado apontando para o banco de dados. O serviço será reiniciado ao final." },
];

const _irDgBase = window.ir;
window.ir = function(telaId, btn) {
  _irDgBase(telaId, btn);
  if (telaId === "tela-downgrade") {
    renderDgAmbList();
    carregarVersoesDg();
    initDgStepper();
  }
};

function initDgStepper() {
  const el = document.getElementById("dg-stepper");
  if (!el) return;
  el.innerHTML = DG_ETAPAS.map(e => `
    <div class="step" id="dgstep-${e.n}">
      <div class="step-dot">${e.n}</div>
      <div class="step-content">
        <div class="step-titulo">${e.titulo}</div>
        <div class="step-sub" id="dgstep-sub-${e.n}">Aguardando...</div>
        <div class="step-help">${e.ajuda}</div>
        <div class="confirm-box" id="dg-confirm-${e.n}">
          <div class="confirm-titulo">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            Confirmar execução desta etapa
          </div>
          <div class="confirm-desc">Verifique o log abaixo e clique em <strong>Confirmar</strong> para executar, ou <strong>Cancelar</strong> para interromper.</div>
          <div class="confirm-btns">
            <button class="btn btn-sm" style="background:#dc2626;color:#fff;" onclick="confirmarDgEtapa()">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
              Confirmar e Executar
            </button>
            <button class="btn btn-danger btn-sm" onclick="cancelarDgEtapa()">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              Cancelar Processo
            </button>
          </div>
        </div>
      </div>
    </div>`).join("");
}

function setDgStep(n, status, sub) {
  const el = document.getElementById(`dgstep-${n}`);
  if (!el) return;
  el.className = `step s-${status}`;
  if (sub) document.getElementById(`dgstep-sub-${n}`).textContent = sub;
}

function showDgConfirm(n, show) {
  const b = document.getElementById(`dg-confirm-${n}`);
  if (b) b.className = `confirm-box${show ? " show" : ""}`;
}

function renderDgAmbList() {
  const el = document.getElementById("dg-amb-list");
  if (!el) return;
  if (!getAmbientes().length) {
    el.innerHTML = `<div class="empty-amb">Nenhum ambiente cadastrado.<br>
      <a href="#" onclick="ir('tela-getAmbientes()',document.getElementById('nav-getAmbientes()'))">Cadastrar agora</a></div>`;
    return;
  }
  el.innerHTML = getAmbientes().map(a => `
    <div class="amb-opt ${dgAmbienteSel === a.id ? "sel" : ""}" onclick="selecionarDgAmb(${a.id})">
      <svg class="ao-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
      </svg>
      <div class="ao-info">
        <div class="ao-nome">${tipoTag(a.tipo)} ${esc(a.nome)}</div>
        <div class="ao-host">${a.app_host || "—"} | BD: ${a.bd_host || "—"}</div>
      </div>
      <svg class="ao-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
    </div>`).join("");
}

function selecionarDgAmb(id) {
  dgAmbienteSel = id;
  renderDgAmbList();
  verificarProntoDg();
}

async function carregarVersoesDg() {
  const el = document.getElementById("dg-versao-list");
  const st = document.getElementById("dg-sync-status");
  if (!el) return;
  el.innerHTML = `<div class="versao-loading"><svg class="spin" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg></div>`;
  try {
    const [rv, rs] = await Promise.all([
      fetch("/api/versoes").then(r => r.json()),
      fetch("/api/versoes/status").then(r => r.json()),
    ]);
    dgVersoes     = rv;
    dgVersoesFilt = [...rv];
    if (st) {
      if (rs.ultima_sync) {
        st.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#15803d" stroke-width="2" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg> Última sync: ${rs.ultima_sync}`;
        st.style.color = "#15803d";
      } else {
        st.textContent = "Clique em Atualizar para buscar versões.";
        st.style.color = "#94a3b8";
      }
    }
    renderVersoesDg();
  } catch(e) {
    el.innerHTML = `<div class="versao-erro">Erro ao carregar versões.</div>`;
  }
}

async function sincronizarVersoesDg() {
  const btn = document.getElementById("dg-btn-sync");
  if (btn) btn.disabled = true;
  await fetch("/api/versoes/sync", { method: "POST" });
  await new Promise(r => setTimeout(r, 800));
  await carregarVersoesDg();
  if (btn) btn.disabled = false;
}

function onFiltroDg() {
  const q = document.getElementById("dg-versao-filtro").value.trim();
  dgVersoesFilt = q ? dgVersoes.filter(v => v.versao.includes(q)) : [...dgVersoes];
  renderVersoesDg();
}

function renderVersoesDg() {
  const el = document.getElementById("dg-versao-list");
  if (!el) return;
  if (!dgVersoes.length) {
    el.innerHTML = `<div class="versao-loading" style="color:#f59e0b">Nenhuma versão. Clique em <strong>Atualizar</strong>.</div>`;
    return;
  }
  if (!dgVersoesFilt.length) {
    el.innerHTML = `<div class="versao-erro">Nenhuma versão para este filtro.</div>`;
    return;
  }
  el.innerHTML = dgVersoesFilt.map((v, i) => `
    <div class="versao-item ${dgVersaoSel && dgVersaoSel.versao === v.versao ? "sel" : ""}" onclick="selecionarVersaoDg(${dgVersoes.indexOf(v)})">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="${v.tem_url ? "#dc2626" : "#94a3b8"}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
      <div style="flex:1">
        <div class="vi-num" style="color:${v.tem_url ? "#991b1b" : "#94a3b8"}">
          Versão ${v.versao}
          ${i === 0 ? '<span style="background:#fee2e2;color:#991b1b;font-size:.68rem;padding:.1rem .45rem;border-radius:4px;font-weight:700;margin-left:.3rem">MAIS RECENTE</span>' : ""}
          ${!v.tem_url ? '<span style="background:#f1f5f9;color:#94a3b8;font-size:.68rem;padding:.1rem .45rem;border-radius:4px;margin-left:.3rem">sem URL</span>' : ""}
        </div>
        <div class="vi-file">${v.arquivo || ""}</div>
      </div>
      <svg class="vi-check" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#dc2626" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
    </div>`).join("");
}

function selecionarVersaoDg(idx) {
  dgVersaoSel = dgVersoes[idx];
  renderVersoesDg();
  verificarProntoDg();
}

function verificarProntoDg() {
  const ok = dgAmbienteSel && dgVersaoSel && dgVersaoSel.tem_url && !dgExecutando;
  const btn = document.getElementById("btn-dg-iniciar");
  if (btn) btn.disabled = !ok;
}

async function iniciarDowngrade() {
  if (!dgAmbienteSel || !dgVersaoSel || dgExecutando) return;

  const amb = getAmbientes().find(a => a.id === dgAmbienteSel);
  const confirmado = await Swal.fire({
    title: "Confirmar Downgrade",
    html: `Reverter <strong>${esc(amb?.nome || "")}</strong> para a versão <strong>${esc(dgVersaoSel.versao)}</strong>?<br><br>
           <span style="font-size:.85rem;color:#dc2626;">⚠ O serviço ficará indisponível durante o processo.</span>`,
    icon: "warning",
    showCancelButton: true,
    confirmButtonText: "Sim, iniciar downgrade",
    cancelButtonText: "Cancelar",
    confirmButtonColor: "#dc2626",
  });
  if (!confirmado.isConfirmed) return;

  dgExecutando = true;
  dgEtapaAtual = 0;
  verificarProntoDg();
  document.getElementById("dg-log-body").innerHTML = "";
  document.getElementById("dg-exec-status").textContent = "";
  initDgStepper();
  dgStatus("Iniciando...", "s-run");
  dgLog("Iniciando sessão de downgrade...");

  const r = await fetch("/api/downgrade/iniciar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ambiente_id:  dgAmbienteSel,
      versao:       dgVersaoSel.versao,
      url_download: dgVersaoSel.url_linux,
      modo_auto:    document.getElementById("dg-modo-auto").checked,
    }),
  });
  const d = await r.json();

  if (!r.ok || !d.sid) {
    dgLog(d.erro || "Erro ao iniciar.", "erro");
    dgStatus("Erro ao iniciar.", "s-err");
    dgExecutando = false;
    verificarProntoDg();
    return;
  }

  dgSessionId = d.sid;
  document.getElementById("dg-exec-status").textContent = `#${d.sid.substring(0, 6)}`;
  ouvirStreamDg();
}

function ouvirStreamDg() {
  const es = new EventSource(`/api/downgrade/stream/${dgSessionId}`);

  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    if (!ev.msg || ev.tipo === "ping") return;

    if (ev.tipo === "controle") {
      if (ev.msg === "__AGUARDA__") {
        showDgConfirm(dgEtapaAtual, true);
        setDgStep(dgEtapaAtual, "active", "Aguardando confirmação...");
        dgStatus(`Etapa ${dgEtapaAtual}: aguardando confirmação`, "s-run");
      } else if (ev.msg === "__FIM_OK__") {
        if (dgEtapaAtual) setDgStep(dgEtapaAtual, "s-done", "Concluído ✓");
        dgStatus("✓ Downgrade concluído com sucesso!", "s-ok");
        document.getElementById("dg-exec-status").textContent = "";
        dgExecutando = false;
        verificarProntoDg();
        es.close();
      } else if (ev.msg === "__FIM_ERRO__") {
        if (dgEtapaAtual) setDgStep(dgEtapaAtual, "s-error", "Erro — veja o log.");
        dgStatus("Downgrade encerrou com erro.", "s-err");
        showDgConfirm(dgEtapaAtual, false);
        dgExecutando = false;
        verificarProntoDg();
        es.close();
      }
      return;
    }

    if (ev.tipo === "etapa") {
      const parts = ev.msg.split("__");
      const n = parseInt(parts[2]);
      const titulo = parts[3] || "";
      if (dgEtapaAtual && dgEtapaAtual !== n) setDgStep(dgEtapaAtual, "s-done", "Concluído ✓");
      dgEtapaAtual = n;
      setDgStep(n, "active", "Preparando...");
      showDgConfirm(n, false);
      dgLog(`━━ ETAPA ${n}: ${titulo} ━━`, "etapa");
      return;
    }

    dgLog(ev.msg, ev.tipo);
  };

  es.onerror = () => {
    dgStatus("Conexão perdida com o servidor.", "s-err");
    dgExecutando = false;
    verificarProntoDg();
    es.close();
  };
}

async function confirmarDgEtapa() {
  showDgConfirm(dgEtapaAtual, false);
  setDgStep(dgEtapaAtual, "active", "Executando...");
  dgStatus(`Executando etapa ${dgEtapaAtual}...`, "s-run");
  await fetch(`/api/downgrade/confirmar/${dgSessionId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
}

async function cancelarDgEtapa() {
  showDgConfirm(dgEtapaAtual, false);
  setDgStep(dgEtapaAtual, "s-error", "Cancelado.");
  dgStatus("Processo cancelado pelo usuário.", "s-err");
  await fetch(`/api/downgrade/confirmar/${dgSessionId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cancelar: true }),
  });
  dgExecutando = false;
  verificarProntoDg();
}

function dgLog(msg, tipo = "info") {
  const body = document.getElementById("dg-log-body");
  if (!body) return;
  const ts = new Date().toLocaleTimeString("pt-BR", { hour:"2-digit", minute:"2-digit", second:"2-digit" });
  const l = document.createElement("div");
  l.className = `log-line log-${tipo}`;
  l.innerHTML = `<span class="log-ts">${ts}</span><span class="log-msg">${esc(msg)}</span>`;
  body.appendChild(l);
  body.scrollTop = body.scrollHeight;
}

function dgStatus(msg, cls) {
  const bar = document.getElementById("dg-status-bar");
  if (!bar) return;
  bar.innerHTML = `<span class="${cls}">${cls === "s-run" ? '<span class="dot"></span>' : ""}${msg}</span>`;
}
