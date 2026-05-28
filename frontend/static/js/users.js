// ── Usuários ──────────────────────────────────────────────────────────────────
let editandoUsuarioId = null;

// Carrega nome do usuário logado no header e preenche dropdown
(async () => {
  try {
    const r = await fetch("/api/auth/me");
    if (r.ok) {
      const d = await r.json();
      // Nome no botão do menu
      const elNome = document.getElementById("h-usuario");
      if (elNome) elNome.textContent = d.nome;
      // Header do dropdown
      const udNome = document.getElementById("ud-nome");
      const udPapel = document.getElementById("ud-papel");
      if (udNome) udNome.textContent = d.nome;
      if (udPapel)
        udPapel.textContent =
          d.papel === "admin" ? "Administrador" : "Operador";
      // Esconde "Gerenciar Usuários" no dropdown se não for admin
      if (d.papel !== "admin") {
        const btn = document.getElementById("ud-usuarios-btn");
        if (btn) btn.style.display = "none";
      }
    }
  } catch {}
})();

async function fazerLogout() {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.href = "/login";
}

// Carrega usuários ao abrir a tela
const _irUsersOrig = typeof window.ir === "function" ? window.ir : null;
window.ir = function (telaId, btn) {
  if (_irUsersOrig) _irUsersOrig(telaId, btn);
  if (telaId === "tela-usuarios") carregarUsuarios();
};

async function carregarUsuarios() {
  const tbody = document.getElementById("usuarios-tbody");
  tbody.innerHTML = `<tr><td colspan="5" style="padding:2rem;text-align:center;color:#94a3b8">
    <svg class="spin" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
  </td></tr>`;

  const r = await fetch("/api/usuarios");
  if (r.status === 403) {
    tbody.innerHTML = `<tr><td colspan="5" style="padding:2rem;text-align:center;color:#dc2626">Acesso negado.</td></tr>`;
    return;
  }
  const usuarios = await r.json();

  if (!usuarios.length) {
    tbody.innerHTML = `<tr><td colspan="5" style="padding:2rem;text-align:center;color:#94a3b8">Nenhum usuário cadastrado.</td></tr>`;
    return;
  }

  tbody.innerHTML = usuarios
    .map((u) => {
      const papelBadge =
        u.papel === "admin"
          ? `<span style="background:#ede9fe;color:#6d28d9;padding:.2rem .6rem;border-radius:5px;font-size:.72rem;font-weight:800;">ADMIN</span>`
          : `<span style="background:#e0f2fe;color:#0369a1;padding:.2rem .6rem;border-radius:5px;font-size:.72rem;font-weight:800;">OPERADOR</span>`;
      const statusBadge = u.ativo
        ? `<span style="background:#f0fdf4;color:#15803d;padding:.2rem .6rem;border-radius:5px;font-size:.72rem;font-weight:700;">● Ativo</span>`
        : `<span style="background:#f1f5f9;color:#94a3b8;padding:.2rem .6rem;border-radius:5px;font-size:.72rem;font-weight:700;">○ Inativo</span>`;
      const criado = u.criado
        ? u.criado.substring(0, 16).replace("T", " ")
        : "—";
      return `
      <tr style="border-bottom:1px solid #f1f5f9;">
        <td style="padding:.75rem 1.2rem;font-weight:600;">${esc(u.username)}</td>
        <td style="padding:.75rem 1.2rem;text-align:center;">${papelBadge}</td>
        <td style="padding:.75rem 1.2rem;text-align:center;">${statusBadge}</td>
        <td style="padding:.75rem 1.2rem;text-align:center;color:#64748b;font-size:.8rem;">${criado}</td>
        <td style="padding:.75rem 1.2rem;text-align:center;">
          <div style="display:flex;gap:.4rem;justify-content:center;">
            <button class="btn btn-ghost btn-sm" onclick="editarUsuario(${u.id},'${esc(u.username)}','${u.papel}',${u.ativo})">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
              Editar
            </button>
            <button class="btn btn-ghost btn-sm" onclick="toggleAtivoUsuario(${u.id},${u.ativo},'${esc(u.username)}')" title="${u.ativo ? "Desativar" : "Ativar"} usuário">
              ${
                u.ativo
                  ? `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="5" width="22" height="14" rx="7"/><circle cx="16" cy="12" r="3"/></svg>`
                  : `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="5" width="22" height="14" rx="7"/><circle cx="8" cy="12" r="3"/></svg>`
              }
            </button>
            <button class="btn btn-danger btn-sm" onclick="deletarUsuario(${u.id},'${esc(u.username)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
            </button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

function abrirModalUsuario() {
  editandoUsuarioId = null;
  document.getElementById("modal-usuario-titulo").innerHTML = `
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0052A3" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
    Novo Usuário`;
  document.getElementById("fu-username").value = "";
  document.getElementById("fu-username").disabled = false;
  document.getElementById("fu-senha").value = "";
  document.getElementById("fu-papel").value = "operador";
  document.getElementById("fu-senha-label").textContent = "Senha *";
  document.getElementById("fu-senha-hint").textContent = "";
  document.getElementById("modal-usuario-overlay").classList.add("open");
}

function editarUsuario(id, username, papel, ativo) {
  editandoUsuarioId = id;
  document.getElementById("modal-usuario-titulo").innerHTML = `
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0052A3" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
    Editar Usuário`;
  document.getElementById("fu-username").value = username;
  document.getElementById("fu-username").disabled = true;
  document.getElementById("fu-senha").value = "";
  document.getElementById("fu-papel").value = papel;
  document.getElementById("fu-senha-label").textContent = "Nova senha";
  document.getElementById("fu-senha-hint").textContent =
    "Deixe em branco para manter a senha atual.";
  document.getElementById("modal-usuario-overlay").classList.add("open");
}

function fecharModalUsuario() {
  document.getElementById("modal-usuario-overlay").classList.remove("open");
  editandoUsuarioId = null;
}

async function salvarUsuario() {
  const username = document.getElementById("fu-username").value.trim();
  const senha = document.getElementById("fu-senha").value;
  const papel = document.getElementById("fu-papel").value;

  if (!editandoUsuarioId && (!username || !senha)) {
    Swal.fire({
      title: "Campos obrigatórios",
      text: "Informe usuário e senha.",
      icon: "warning",
    });
    return;
  }

  let url = "/api/usuarios";
  let method = "POST";
  let body = { username, senha, papel };

  if (editandoUsuarioId) {
    url = `/api/usuarios/${editandoUsuarioId}`;
    method = "PUT";
    body = { papel };
    if (senha) body.senha = senha;
  }

  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = await r.json();

  if (r.ok) {
    fecharModalUsuario();
    await carregarUsuarios();
    Swal.fire({
      title: "Salvo!",
      text: "Usuário salvo com sucesso.",
      icon: "success",
      timer: 1500,
      showConfirmButton: false,
    });
  } else {
    Swal.fire({
      title: "Erro",
      text: d.erro || "Erro ao salvar.",
      icon: "error",
    });
  }
}

async function toggleAtivoUsuario(id, ativoAtual, username) {
  const novoEstado = !ativoAtual;
  const acao = novoEstado ? "ativar" : "desativar";
  const res = await Swal.fire({
    title: `${novoEstado ? "Ativar" : "Desativar"} "${username}"?`,
    icon: "question",
    showCancelButton: true,
    confirmButtonText: `Sim, ${acao}`,
    cancelButtonText: "Cancelar",
  });
  if (!res.isConfirmed) return;
  await fetch(`/api/usuarios/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ativo: novoEstado }),
  });
  await carregarUsuarios();
}

async function deletarUsuario(id, username) {
  const res = await Swal.fire({
    title: `Remover "${username}"?`,
    text: "Esta ação não pode ser desfeita.",
    icon: "warning",
    showCancelButton: true,
    confirmButtonText: "Sim, remover",
    cancelButtonText: "Cancelar",
    confirmButtonColor: "#dc2626",
  });
  if (!res.isConfirmed) return;
  const r = await fetch(`/api/usuarios/${id}`, { method: "DELETE" });
  const d = await r.json();
  if (!r.ok) {
    Swal.fire({ title: "Erro", text: d.erro, icon: "error" });
    return;
  }
  await carregarUsuarios();
}

function toggleUserMenu() {
  const drop = document.getElementById("user-dropdown");
  const btn = document.getElementById("user-menu-btn");
  const open = drop.classList.toggle("open");
  btn.setAttribute("aria-expanded", open);
}
// Fecha ao clicar fora
document.addEventListener("click", function (e) {
  const wrap = document.getElementById("user-menu-wrap");
  if (wrap && !wrap.contains(e.target)) {
    document.getElementById("user-dropdown").classList.remove("open");
    document
      .getElementById("user-menu-btn")
      .setAttribute("aria-expanded", "false");
  }
});
function irUsuarios() {
  document.getElementById("user-dropdown").classList.remove("open");
  document
    .getElementById("user-menu-btn")
    .setAttribute("aria-expanded", "false");
  ir("tela-usuarios", { classList: { add: () => {}, remove: () => {} } });
  // Marca nav ativo se existir
  document
    .querySelectorAll(".nav-btn")
    .forEach((b) => b.classList.remove("active"));
}
