// ── Backup ────────────────────────────────────────────────────────────────────
let backupAmbienteSel = null;
let backupExecutando  = false;

const BACKUP_ETAPAS = [
  { n: 1, titulo: "Verificar conexão com o banco", ajuda: "O sistema confirma que consegue acessar o container PostgreSQL via docker exec." },
  { n: 2, titulo: "Executar pg_dump", ajuda: "O dump do banco é gerado em formato compactado (.dump) e salvo na pasta backup_dbesus/." },
  { n: 3, titulo: "Registrar backup", ajuda: "O arquivo gerado é verificado e o registro é salvo no histórico do sistema." },
];

// ── Nav override ──────────────────────────────────────────────────────────────
const _irBackupBase = window.ir;
window.ir = function(telaId, btn) {
  _irBackupBase(telaId, btn);
  if (telaId === "tela-backup") {
    renderBackupAmbList();
    carregarHistoricoBackup();
    initBackupStepper();
  }
};

// ── Stepper ───────────────────────────────────────────────────────────────────
function initBackupStepper() {
  const el = document.getElementById("backup-stepper");
  if (!el) return;
  el.innerHTML = BACKUP_ETAPAS.map(e => `
    <div class="step" id="bstep-${e.n}">
      <div class="step-dot">${e.n}</div>
      <div class="step-content">
        <div class="step-titulo">${e.titulo}</div>
        <div class="step-sub" id="bstep-sub-${e.n}">Aguardando...</div>
        <div class="step-help">${e.ajuda}</div>
      </div>
    </div>`).join("");
}

function setBStep(n, status, sub) {
  const el = document.getElementById(`bstep-${n}`);
  if (!el) return;
  el.className = `step s-${status}`;
  if (sub) document.getElementById(`bstep-sub-${n}`).textContent = sub;
}

// ── Ambiente list ─────────────────────────────────────────────────────────────
function renderBackupAmbList() {
  const el = document.getElementById("backup-amb-list");
  if (!el) return;
  if (!ambientes.length) {
    el.innerHTML = `<div class="empty-amb">Nenhum ambiente cadastrado.<br>
      <a href="#" onclick="ir('tela-ambientes',document.getElementById('nav-ambientes'))">Cadastrar agora</a></div>`;
    return;
  }
  el.innerHTML = ambientes.map(a => `
    <div class="amb-opt ${backupAmbienteSel === a.id ? "sel" : ""}" onclick="selecionarBackupAmb(${a.id})">
      <svg class="ao-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/>
        <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>
      </svg>
      <div class="ao-info">
        <div class="ao-nome">${tipoTag(a.tipo)} ${esc(a.nome)}</div>
        <div class="ao-host">BD: ${a.bd_host || "—"} · ${a.db_name || "—"}</div>
      </div>
      <svg class="ao-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
    </div>`).join("");
}

function selecionarBackupAmb(id) {
  backupAmbienteSel = id;
  renderBackupAmbList();
  const btn = document.getElementById("btn-backup-iniciar");
  if (btn) btn.disabled = backupExecutando;
  carregarHistoricoBackup();
}

// ── Log helpers ───────────────────────────────────────────────────────────────
function bkpLog(msg, tipo = "info") {
  const body = document.getElementById("backup-log-body");
  if (!body) return;
  const ts = new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const l = document.createElement("div");
  l.className = `log-line log-${tipo}`;
  l.innerHTML = `<span class="log-ts">${ts}</span><span class="log-msg">${esc(msg)}</span>`;
  body.appendChild(l);
  body.scrollTop = body.scrollHeight;
}

function bkpStatus(msg, cls) {
  const bar = document.getElementById("backup-status-bar");
  if (!bar) return;
  bar.innerHTML = `<span class="${cls}">${cls === "s-run" ? '<span class="dot"></span>' : ""}${msg}</span>`;
}

// ── Iniciar backup ────────────────────────────────────────────────────────────
async function iniciarBackup() {
  if (!backupAmbienteSel || backupExecutando) return;

  const amb = ambientes.find(a => a.id === backupAmbienteSel);
  const confirmado = await Swal.fire({
    title: "Confirmar Backup",
    html: `Iniciar backup do banco <strong>${esc(amb?.db_name || amb?.nome || "")}</strong> no ambiente <strong>${esc(amb?.nome || "")}</strong>?`,
    icon: "question",
    showCancelButton: true,
    confirmButtonText: "Sim, iniciar backup",
    cancelButtonText: "Cancelar",
    confirmButtonColor: "#0052a3",
  });
  if (!confirmado.isConfirmed) return;

  // Reset UI
  backupExecutando = true;
  document.getElementById("btn-backup-iniciar").disabled = true;
  document.getElementById("backup-log-body").innerHTML = "";
  document.getElementById("backup-exec-status").textContent = "";
  initBackupStepper();
  bkpStatus("Iniciando...", "s-run");
  bkpLog("Iniciando sessão de backup...");

  const r = await fetch("/api/backup/iniciar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ambiente_id: backupAmbienteSel }),
  });
  const d = await r.json();

  if (!r.ok || !d.sid) {
    bkpLog(d.erro || "Erro ao iniciar backup.", "erro");
    bkpStatus("Erro ao iniciar.", "s-err");
    backupExecutando = false;
    document.getElementById("btn-backup-iniciar").disabled = false;
    return;
  }

  document.getElementById("backup-exec-status").textContent = `#${d.sid.substring(0, 6)}`;

  // ── SSE stream ──
  let etapaAtual = 0;
  const es = new EventSource(`/api/backup/stream/${d.sid}`);

  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (!ev.msg || ev.tipo === "ping") return;

    if (ev.tipo === "controle") {
      if (ev.msg === "__FIM_OK__") {
        if (etapaAtual) setBStep(etapaAtual, "s-done", "Concluído ✓");
        bkpStatus("✓ Backup concluído com sucesso!", "s-ok");
        document.getElementById("backup-exec-status").textContent = "";
        backupExecutando = false;
        document.getElementById("btn-backup-iniciar").disabled = false;
        carregarHistoricoBackup();
        es.close();
      } else if (ev.msg === "__FIM_ERRO__") {
        if (etapaAtual) setBStep(etapaAtual, "s-error", "Erro — veja o log.");
        bkpStatus("Backup encerrou com erro.", "s-err");
        backupExecutando = false;
        document.getElementById("btn-backup-iniciar").disabled = false;
        es.close();
      }
      return;
    }

    if (ev.tipo === "etapa") {
      const parts = ev.msg.split("__");
      const n = parseInt(parts[2]);
      const titulo = parts[3] || "";
      if (etapaAtual && etapaAtual !== n) setBStep(etapaAtual, "s-done", "Concluído ✓");
      etapaAtual = n;
      setBStep(n, "active", "Executando...");
      bkpLog(`━━ ETAPA ${n}: ${titulo} ━━`, "etapa");
      return;
    }

    bkpLog(ev.msg, ev.tipo);
  };

  es.onerror = () => {
    bkpStatus("Conexão perdida com o servidor.", "s-err");
    backupExecutando = false;
    document.getElementById("btn-backup-iniciar").disabled = false;
    es.close();
  };
}

// ── Histórico ─────────────────────────────────────────────────────────────────
async function carregarHistoricoBackup() {
  const tbody = document.getElementById("backup-historico-tbody");
  if (!tbody) return;

  const url = backupAmbienteSel
    ? `/api/backup/historico?ambiente_id=${backupAmbienteSel}`
    : "/api/backup/historico";

  tbody.innerHTML = `<tr><td colspan="5" style="padding:1.5rem;text-align:center;color:#94a3b8">
    <svg class="spin" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
  </td></tr>`;

  const r = await fetch(url);
  const rows = await r.json();

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="5" style="padding:1.5rem;text-align:center;color:#94a3b8">Nenhum backup realizado ainda.</td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map(b => {
    const tamanho = b.tamanho_kb >= 1024
      ? `${(b.tamanho_kb / 1024).toFixed(1)} MB`
      : `${b.tamanho_kb} KB`;
    const statusBadge = b.status === "ok"
      ? `<span style="background:#f0fdf4;color:#15803d;padding:.15rem .5rem;border-radius:4px;font-size:.72rem;font-weight:700;">✓ OK</span>`
      : `<span style="background:#fef2f2;color:#dc2626;padding:.15rem .5rem;border-radius:4px;font-size:.72rem;font-weight:700;">✗ Erro</span>`;
    return `
      <tr style="border-bottom:1px solid #f1f5f9;">
        <td style="padding:.65rem 1rem;font-family:monospace;font-size:.78rem;color:#0052a3;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(b.arquivo)}">${esc(b.arquivo)}</td>
        <td style="padding:.65rem 1rem;font-size:.83rem;">${esc(b.ambiente_nome || "—")}</td>
        <td style="padding:.65rem 1rem;text-align:center;">${statusBadge}</td>
        <td style="padding:.65rem 1rem;text-align:center;font-size:.78rem;color:#64748b;white-space:nowrap;">${(b.criado || "").substring(0, 16).replace("T", " ")}</td>
        <td style="padding:.65rem 1rem;text-align:center;">
          <button class="btn btn-danger btn-sm" onclick="deletarBackup('${esc(b.arquivo)}')" title="Remover backup">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
              <path d="M10 11v6"/><path d="M14 11v6"/>
            </svg>
          </button>
        </td>
      </tr>`;
  }).join("");
}

async function deletarBackup(arquivo) {
  const res = await Swal.fire({
    title: "Remover backup?",
    html: `O arquivo <code style="font-size:.85rem">${esc(arquivo)}</code> será deletado permanentemente.`,
    icon: "warning",
    showCancelButton: true,
    confirmButtonText: "Sim, remover",
    cancelButtonText: "Cancelar",
    confirmButtonColor: "#dc2626",
  });
  if (!res.isConfirmed) return;
  const r = await fetch("/api/backup/deletar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ arquivo }),
  });
  const d = await r.json();
  if (!r.ok) {
    Swal.fire({ title: "Erro", text: d.erro, icon: "error" });
    return;
  }
  await carregarHistoricoBackup();
}
