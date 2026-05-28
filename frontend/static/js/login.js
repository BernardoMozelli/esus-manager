const btnLogin = document.getElementById("btn-login");
const erroBox = document.getElementById("login-erro");
const erroMsg = document.getElementById("login-erro-msg");
const togglePwd = document.getElementById("toggle-pwd");
const senhaInp = document.getElementById("senha");

togglePwd.addEventListener("click", () => {
  const visible = senhaInp.type === "text";
  senhaInp.type = visible ? "password" : "text";
  togglePwd.innerHTML = visible
    ? `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`
    : `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
});

async function doLogin() {
  const username = document.getElementById("username").value.trim();
  const senha = senhaInp.value;
  if (!username || !senha) {
    erroMsg.textContent = "Preencha usuário e senha.";
    erroBox.classList.add("show");
    return;
  }
  btnLogin.disabled = true;
  btnLogin.innerHTML = `<svg class="spin" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Entrando...`;
  erroBox.classList.remove("show");
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, senha }),
    });
    const d = await r.json();
    if (r.ok) {
      window.location.href = "/";
    } else {
      erroMsg.textContent = d.erro || "Usuário ou senha inválidos.";
      erroBox.classList.add("show");
    }
  } catch {
    erroMsg.textContent = "Erro de conexão. Tente novamente.";
    erroBox.classList.add("show");
  } finally {
    btnLogin.disabled = false;
    btnLogin.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg> Entrar`;
  }
}

btnLogin.addEventListener("click", doLogin);
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});

// Spin animation
const style = document.createElement("style");
style.textContent =
  ".spin{animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}";
document.head.appendChild(style);
