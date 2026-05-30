from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for
from functools import wraps
import hashlib, secrets
import paramiko, requests, re, threading, queue, time, datetime, json, uuid, urllib3, sqlite3, os

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Frontend sempre em /app/frontend/
_base = os.path.dirname(__file__)
FRONTEND_DIR = os.path.join(_base, "frontend")

app = Flask(
    __name__,
    template_folder=os.path.join(FRONTEND_DIR, "templates"),
    static_folder=os.path.join(FRONTEND_DIR, "static"),
)
app.secret_key = "esus-pmsl-2024"

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "config.db"))
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

# ─── Banco ────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS ambientes (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            nome   TEXT NOT NULL,
            tipo   TEXT NOT NULL DEFAULT 'homologacao',
            criado TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS srv_app (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ambiente_id INTEGER NOT NULL REFERENCES ambientes(id) ON DELETE CASCADE,
            host        TEXT NOT NULL,
            porta       INTEGER NOT NULL DEFAULT 22,
            usuario     TEXT NOT NULL,
            senha       TEXT NOT NULL DEFAULT '',
            esus_dir    TEXT NOT NULL DEFAULT '/opt/e-SUS',
            service     TEXT NOT NULL DEFAULT 'e-SUS-PEC',
            usa_docker  INTEGER NOT NULL DEFAULT 0,
            container   TEXT NOT NULL DEFAULT ''
        );
        -- Migração: adiciona container em srv_app se não existir
        CREATE TABLE IF NOT EXISTS srv_bd (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ambiente_id INTEGER NOT NULL REFERENCES ambientes(id) ON DELETE CASCADE,
            host        TEXT NOT NULL,
            porta       INTEGER NOT NULL DEFAULT 22,
            usuario     TEXT NOT NULL,
            senha       TEXT NOT NULL DEFAULT '',
            db_name     TEXT NOT NULL,
            container      TEXT NOT NULL DEFAULT 'postgresql-db-1',
            db_host_interno TEXT NOT NULL DEFAULT '',
            pg_usuario      TEXT NOT NULL DEFAULT 'postgres',
            pg_senha        TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS backups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ambiente_id INTEGER NOT NULL REFERENCES ambientes(id) ON DELETE CASCADE,
            db_name     TEXT NOT NULL,
            arquivo     TEXT NOT NULL,
            tamanho_kb  INTEGER NOT NULL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'ok',
            criado      TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS versoes (
            versao      TEXT PRIMARY KEY,
            url_linux   TEXT,
            url_windows TEXT,
            data_pub    TEXT,
            descoberta  TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS usuarios (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            senha_hash TEXT NOT NULL,
            papel    TEXT NOT NULL DEFAULT 'operador',
            criado   TEXT DEFAULT (datetime('now','localtime')),
            ativo    INTEGER NOT NULL DEFAULT 1
        );
        """)

# ─── Auth helpers ─────────────────────────────────────────────────────────────

def hash_senha(senha):
    return hashlib.sha256(senha.encode()).hexdigest()

# Migração: garante que a coluna container existe em srv_app (bancos criados antes desta versão)
def migrar_db():
    with get_db() as db:
        for sql in [
            "ALTER TABLE srv_app ADD COLUMN container TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE srv_bd  ADD COLUMN db_host_interno TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE srv_bd  ADD COLUMN pg_usuario TEXT NOT NULL DEFAULT 'postgres'",
            "ALTER TABLE srv_bd  ADD COLUMN pg_senha TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                db.execute(sql)
            except Exception:
                pass  # Coluna já existe

def seed_admin():
    """Garante que o usuário administrador padrão existe."""
    with get_db() as db:
        if db.execute("SELECT id FROM usuarios WHERE username='administrador'").fetchone():
            return
        db.execute(
            "INSERT INTO usuarios (username, senha_hash, papel) VALUES (?,?,?)",
            ("administrador", hash_senha("6p4d0b@2026"), "admin")
        )

def login_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("usuario_id"):
            if request.is_json:
                return jsonify({"erro": "Não autenticado"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def seed_docker():
    """Pré-cadastra o ambiente do docker-compose se ainda não existir."""
    with get_db() as db:
        if db.execute("SELECT id FROM ambientes WHERE nome='e-SUS Local (Docker)'").fetchone():
            return
        cur = db.execute(
            "INSERT INTO ambientes (nome, tipo) VALUES (?,?)",
            ("e-SUS Local (Docker)", "homologacao")
        )
        aid = cur.lastrowid
        db.execute("""
            INSERT INTO srv_app (ambiente_id, host, porta, usuario, senha, esus_dir, service, usa_docker, container)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (aid, "localhost", 22, "root", "", "/opt/e-SUS", "e-SUS-PEC", 1, ""))
        db.execute("""
            INSERT INTO srv_bd (ambiente_id, host, porta, usuario, senha, db_name, container)
            VALUES (?,?,?,?,?,?,?)
        """, (aid, "localhost", 22, "root", "rootpassword", "novosgar", "esus_dblocal"))

init_db()
migrar_db()
seed_docker()
seed_admin()


# ─── Helpers DB ───────────────────────────────────────────────────────────────

def row_to_dict(row):
    return dict(row) if row else None

def get_ambiente_full(aid):
    with get_db() as db:
        amb = row_to_dict(db.execute("SELECT * FROM ambientes WHERE id=?", (aid,)).fetchone())
        if not amb:
            return None
        amb["srv_app"] = row_to_dict(db.execute("SELECT * FROM srv_app WHERE ambiente_id=?", (aid,)).fetchone())
        amb["srv_bd"]  = row_to_dict(db.execute("SELECT * FROM srv_bd  WHERE ambiente_id=?", (aid,)).fetchone())
        return amb

# ─── Sessões ──────────────────────────────────────────────────────────────────

sessoes = {}

# Timeout para aguardar confirmação manual (30 min)
CONFIRM_TIMEOUT = 1800

def nova_sessao(modo_auto=False):
    sid = str(uuid.uuid4())
    sessoes[sid] = {
        "log_queue": queue.Queue(),
        "aguardando": threading.Event(),
        "cancelado": False,
        "finalizado": False,
        "modo_auto": modo_auto,
    }
    return sid

def log(sid, msg, tipo="info"):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    sessoes[sid]["log_queue"].put(json.dumps({"ts": ts, "msg": msg, "tipo": tipo}))

def aguardar(sid):
    """
    No modo autônomo, confirma imediatamente sem esperar o usuário.
    No modo manual, aguarda até CONFIRM_TIMEOUT segundos.
    Se o timeout expirar, cancela automaticamente para não travar a thread.
    """
    s = sessoes[sid]
    if s.get("modo_auto"):
        # Modo autônomo: prossegue sem interação
        return True
    s["aguardando"].clear()
    log(sid, "__AGUARDA__", "controle")
    confirmado = s["aguardando"].wait(timeout=CONFIRM_TIMEOUT)
    if not confirmado:
        # Timeout: cancela para não deixar thread presa
        s["cancelado"] = True
        log(sid, f"Timeout: nenhuma confirmação em {CONFIRM_TIMEOUT//60} minutos. Operação cancelada.", "warn")
        return False
    return not s["cancelado"]

# ─── SSH ──────────────────────────────────────────────────────────────────────

def ssh_connect(host, porta, usuario, senha):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, port=porta, username=usuario, password=senha, timeout=30)
    # Keepalive a cada 30s para evitar queda durante operações longas (backup, download)
    c.get_transport().set_keepalive(30)
    return c

def _is_download_noise(line):
    """Detecta linhas de progresso verboso do wget/curl."""
    if re.match(r'^\s*\d+K[\s.]+\d+%', line):
        return True
    if re.match(r'^\s*\d+%\[', line):
        return True
    if re.match(r'^[# ]+\d+\.\d+%', line):
        return True
    return False

def _parse_wget_percent(line):
    """Extrai porcentagem de uma linha de progresso do wget, ou None."""
    m = re.search(r'(\d+)%', line)
    return int(m.group(1)) if m else None

def _ssh_exec_raw(client, cmd, timeout=15):
    """Executa comando SSH e retorna (stdin, stdout, stderr, returncode) sem logar no SSE."""
    try:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        stdout.channel.settimeout(timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        rc  = stdout.channel.recv_exit_status()
        return stdin, out, err, rc
    except Exception as e:
        return None, "", str(e), 1

def ssh_exec(sid, client, cmd, timeout=60, is_download=False):
    """
    Executa um comando SSH e envia o output para o log da sessão.
    is_download=True: filtra linhas ruidosas do wget/curl, mostrando
    apenas marcos de 10% em 10%.
    """
    cmd = re.sub(r'^sudo\s+', '', cmd.strip())
    
    # Retry mechanism for SSH session
    max_retries = 3
    chan = None
    for attempt in range(max_retries):
        try:
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise paramiko.SSHException("Transport is not active")
            chan = transport.open_session()
            break
        except paramiko.SSHException as e:
            if attempt < max_retries - 1:
                log(sid, f"  [Aviso] Sessão SSH falhou ({e}). Tentando reconectar (tentativa {attempt + 1}/{max_retries})...", "warn")
                time.sleep(2)
                # Attempt to reconnect if transport is dead
                try:
                    host, port = client.get_transport().getpeername()
                    username = client.get_transport().get_username()
                    # We can't easily get the password here, so we rely on the caller to manage connection state if it fully drops
                    # But for simple session drops, transport might still be reconnectable or just needs a new session
                except Exception:
                    pass
            else:
                log(sid, f"  [Erro] Falha ao abrir sessão SSH após {max_retries} tentativas: {e}", "warn")
                return "", f"SSH session error: {e}", 1

    if not chan:
        return "", "SSH session could not be established", 1

    chan.settimeout(timeout)
    chan.exec_command(cmd)

    out = []
    err = []
    deadline = time.time() + timeout
    last_pct_logged = -1
    last_progress_log = time.time()
    buf = ""

    def _flush_buf():
        nonlocal buf, last_pct_logged, last_progress_log
        parts = re.split(r'[\r\n]', buf)
        buf = parts[-1]
        for raw in parts[:-1]:
            l = raw.rstrip()
            if not l:
                continue
            if is_download and _is_download_noise(l):
                pct = _parse_wget_percent(l)
                if pct is not None:
                    bucket = (pct // 10) * 10
                    now = time.time()
                    if bucket > last_pct_logged or (now - last_progress_log) >= 30:
                        last_pct_logged = bucket
                        last_progress_log = now
                        log(sid, f"  ↓ Progresso: {pct}%", "cmd")
            else:
                out.append(l)
                log(sid, f"  {l}", "cmd")

    while True:
        if time.time() > deadline:
            log(sid, f"  [timeout após {timeout}s]", "warn")
            chan.close()
            return "\n".join(out), "\n".join(err), 1

        if chan.recv_ready():
            data = chan.recv(8192).decode(errors="replace")
            buf += data
            _flush_buf()

        while chan.recv_stderr_ready():
            data = chan.recv_stderr(4096).decode(errors="replace")
            for line in re.split(r'[\r\n]', data):
                l = line.rstrip()
                if l and "[sudo]" not in l:
                    if is_download and _is_download_noise(l):
                        pct = _parse_wget_percent(l)
                        if pct is not None:
                            bucket = (pct // 10) * 10
                            now = time.time()
                            if bucket > last_pct_logged or (now - last_progress_log) >= 30:
                                last_pct_logged = bucket
                                last_progress_log = now
                                log(sid, f"  ↓ Progresso: {pct}%", "cmd")
                    else:
                        err.append(l)
                        log(sid, f"  {l}", "warn")

        if chan.exit_status_ready():
            while chan.recv_ready():
                data = chan.recv(8192).decode(errors="replace")
                buf += data
            buf += "\n"
            _flush_buf()
            break

        time.sleep(0.2)

    code = chan.recv_exit_status()
    chan.close()
    return "\n".join(out), "\n".join(err), code

# ─── e-SUS: descoberta de versões e URLs ─────────────────────────────────────

BLOG_URL  = "https://sisaps.saude.gov.br/sistemas/esusaps/blog/"
JAR_BASE  = "https://arquivos.esusaps.ufsc.br/PEC"

# Estado do processo de sincronização
_sync_status = {"rodando": False, "ultima_sync": None, "erro": None}

# Script Python que roda dentro do servidor remoto via SSH
# Acessa o site do e-SUS (sem bloqueio de IP), lista versões e captura URLs
# Script de sync carregado do arquivo sync_script.py
with open(os.path.join(os.path.dirname(__file__), "sync_script.py")) as _f:
    SCRIPT_SYNC = _f.read()


def _get_ssh_para_sync():
    """
    Pega credenciais SSH do primeiro ambiente cadastrado.
    Tenta o host cadastrado primeiro; se for um IP que não responde,
    tenta o nome do container como fallback.
    """
    with get_db() as db:
        row = db.execute("""
            SELECT sa.host, sa.porta, sa.usuario, sa.senha, a.nome
            FROM srv_app sa
            JOIN ambientes a ON a.id = sa.ambiente_id
            ORDER BY a.id LIMIT 1
        """).fetchone()
    if not row:
        return None
    creds = row_to_dict(row)

    # Se o host parece um IP (não hostname), tenta conectar
    # Se falhar, tenta nomes de container conhecidos como fallback
    import re as _re
    if _re.match(r'^\d+\.\d+\.\d+\.\d+$', creds["host"]):
        # Testa conexão rápida; se falhar tenta alternativas
        try:
            import socket
            sock = socket.create_connection((creds["host"], creds["porta"]), timeout=3)
            sock.close()
            return creds  # IP funciona, usa ele
        except Exception:
            # IP não responde — tenta nome do container
            for fallback in ["esus_localapp", "esus-app", "localhost"]:
                try:
                    sock = socket.create_connection((fallback, creds["porta"]), timeout=3)
                    sock.close()
                    creds["host"] = fallback
                    return creds
                except Exception:
                    continue
    return creds


def sincronizar_versoes(forcar=False):
    """
    Conecta via SSH no servidor de aplicação, executa scraping do blog do e-SUS
    e salva as versões/URLs encontradas no banco local.
    Roda em background a cada hora.
    """
    global _sync_status
    if _sync_status["rodando"] and not forcar:
        return

    def _rodar():
        global _sync_status
        _sync_status["rodando"] = True
        _sync_status["erro"] = None
        try:
            creds = _get_ssh_para_sync()
            if not creds:
                _sync_status["erro"] = "Nenhum ambiente cadastrado para usar como proxy de scraping."
                return

            try:
                client = ssh_connect(creds["host"], creds["porta"], creds["usuario"], creds["senha"])
            except Exception as e:
                _sync_status["erro"] = (
                    f"Não foi possível conectar via SSH em {creds['host']}:{creds['porta']}. "
                    f"Verifique se o IP/hostname do ambiente está correto e se o SSH está ativo. "
                    f"Detalhe: {e}"
                )
                return
            try:
                # Envia o script via stdin para evitar problemas com caracteres especiais no shell
                chan = client.get_transport().open_session()
                chan.settimeout(600)  # 10 min — inclui instalação do Playwright se necessário
                chan.exec_command("python3")
                chan.sendall(SCRIPT_SYNC.encode())
                chan.shutdown_write()  # Fecha stdin — sinaliza EOF para o python3

                out_buf = ""
                err_buf = ""
                deadline = time.time() + 600
                while True:
                    if time.time() > deadline:
                        break
                    if chan.recv_ready():
                        out_buf += chan.recv(8192).decode(errors="replace")
                    if chan.recv_stderr_ready():
                        err_buf += chan.recv_stderr(4096).decode(errors="replace")
                    if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                        break
                    time.sleep(0.3)

                chan.close()
                client.close()

                # Processa resultado JSON
                linhas = [l.strip() for l in out_buf.strip().splitlines() if l.strip()]
                if not linhas:
                    err_resumo = err_buf.strip()[:200] if err_buf.strip() else "nenhuma"
                    _sync_status["erro"] = f"Script remoto sem saída. Erro: {err_resumo}"
                    return

                json_line = next((l for l in reversed(linhas) if l.startswith("[") or l.startswith("{")), None)
                if not json_line:
                    saida = " | ".join(linhas[:8])
                    erro_extra = err_buf.strip()[:200] if err_buf.strip() else ""
                    _sync_status["erro"] = f"Saída inesperada: {saida[:300]}" + (f" | Stderr: {erro_extra}" if erro_extra else "")
                    return

                dados = json.loads(json_line)

                # Novo formato: {"resultado": [...], "debug": [...]}
                debug_info = []
                if isinstance(dados, dict) and "resultado" in dados:
                    debug_info = dados.get("debug", [])
                    _sync_status["debug"] = debug_info
                    if "erro_geral" in dados:
                        _sync_status["erro"] = f"{dados['erro_geral']} | debug: {' | '.join(debug_info[:5])}"
                        return
                    dados = dados["resultado"]
                elif isinstance(dados, dict) and "erro_geral" in dados:
                    debug_info = dados.get("debug", [])
                    _sync_status["erro"] = f"{dados['erro_geral']}" + (f" | {' | '.join(debug_info[:5])}" if debug_info else "")
                    return

                novas = 0
                atualizadas = 0
                with get_db() as db:
                    for item in dados:
                        versao      = item.get("versao")
                        url_linux   = item.get("url_linux")
                        url_windows = item.get("url_windows")
                        if not versao:
                            continue
                        existe = db.execute("SELECT versao, url_linux FROM versoes WHERE versao=?", (versao,)).fetchone()
                        if not existe:
                            db.execute(
                                "INSERT INTO versoes (versao, url_linux, url_windows) VALUES (?,?,?)",
                                (versao, url_linux, url_windows)
                            )
                            novas += 1
                        elif url_linux and not existe["url_linux"]:
                            db.execute(
                                "UPDATE versoes SET url_linux=?, url_windows=? WHERE versao=?",
                                (url_linux, url_windows, versao)
                            )
                            atualizadas += 1

                _sync_status["ultima_sync"] = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
                _sync_status["novas"] = novas
                _sync_status["atualizadas"] = atualizadas
                if debug_info:
                    _sync_status["debug_ultimo"] = debug_info

            except Exception as e:
                _sync_status["erro"] = str(e)
                try: client.close()
                except: pass

        except Exception as e:
            _sync_status["erro"] = str(e)
        finally:
            _sync_status["rodando"] = False

    threading.Thread(target=_rodar, daemon=True).start()


def _loop_sync():
    """Loop que roda em background, sincronizando a cada 1 hora."""
    time.sleep(10)  # Aguarda sistema inicializar
    while True:
        sincronizar_versoes(forcar=True)
        time.sleep(3600)  # 1 hora

threading.Thread(target=_loop_sync, daemon=True).start()


def buscar_url_versao(versao, url_manual=None, sid=None):
    """Resolve URL de download. Prioridade: manual > banco > hashes estáticos."""
    if url_manual:
        return url_manual

    with get_db() as db:
        row = db.execute("SELECT url_linux FROM versoes WHERE versao=?", (versao,)).fetchone()
        if row and row["url_linux"]:
            return row["url_linux"]

    return None


def buscar_versoes_disponiveis():
    """Retorna versões do banco >= 5.4.30, ordenadas da mais recente para a mais antiga."""
    with get_db() as db:
        rows = db.execute("""
            SELECT versao, url_linux, descoberta
            FROM versoes
            ORDER BY versao DESC
        """).fetchall()

    def versao_valida(v):
        try:
            partes = [int(x) for x in v.split(".")]
            return partes >= [5, 4, 30]
        except:
            return False

    return [
        {
            "versao":    r["versao"],
            "arquivo":   f"eSUS-AB-PEC-{r['versao']}-Linux64.jar",
            "tem_url":   bool(r["url_linux"]),
            "url_linux": r["url_linux"],
            "descoberta": r["descoberta"],
        }
        for r in rows if versao_valida(r["versao"])
    ]



# ─── Thread de atualização ────────────────────────────────────────────────────

def restart_servico(sid, ssh, app_s, container_conhecido=None):
    """Reinicia o e-SUS PEC.
    Se SSH está dentro de um container: mata o processo Java e o entrypoint reinicia.
    Se SSH está no host: usa docker restart ou systemctl.
    """
    service = app_s.get("service", "e-SUS-PEC")
    log(sid, "Reiniciando serviço do e-SUS PEC...", "cmd")

    # Detecta se estamos dentro de um container (sem docker disponível)
    _, docker_check, _, _ = _ssh_exec_raw(ssh, "which docker 2>/dev/null || echo NO_DOCKER", timeout=5)
    dentro_container = "NO_DOCKER" in docker_check or not docker_check.strip()

    if dentro_container:
        # Dentro do container: mata o Java e o entrypoint (standalone.sh) reinicia
        log(sid, "  Modo container: encerrando processo Java para reinício automático...", "cmd")
        ssh_exec(sid, ssh, "pkill -f 'java' 2>/dev/null 2>/dev/null || pkill -9 -f standalone 2>/dev/null || true", timeout=15)
        log(sid, "  Processo encerrado — o container reiniciará automaticamente.", "ok")
        time.sleep(5)
        return

    # No host: tenta docker restart primeiro
    if container_conhecido:
        _, _, rc = ssh_exec(sid, ssh, "docker restart '{}'".format(container_conhecido), timeout=90)
        if rc == 0:
            log(sid, "  Container '{}' reiniciado.".format(container_conhecido), "ok")
            time.sleep(8)
            return

    # Descobre container pelo nome do serviço
    for nome in [service, "e-SUS-PEC", "esus-pec", "esus_pec"]:
        _, out, _, _ = _ssh_exec_raw(ssh,
            "docker ps --filter name={} --format '{{{{.Names}}}}' 2>/dev/null | head -1".format(nome),
            timeout=10
        )
        nome_real = out.strip()
        if nome_real:
            _, _, rc2 = ssh_exec(sid, ssh, "docker restart '{}'".format(nome_real), timeout=90)
            if rc2 == 0:
                log(sid, "  Container '{}' reiniciado.".format(nome_real), "ok")
                time.sleep(8)
                return

    # Fallback: systemctl / service
    _, _, rc3 = ssh_exec(sid, ssh, "systemctl start {}".format(service), timeout=30)
    if rc3 == 0:
        log(sid, "  Serviço '{}' iniciado via systemctl.".format(service), "ok")
        return
    _, _, rc4 = ssh_exec(sid, ssh, "service {} start".format(service), timeout=30)
    if rc4 == 0:
        log(sid, "  Serviço '{}' iniciado via service.".format(service), "ok")
        return
    log(sid, "  Aviso: não foi possível reiniciar automaticamente. Reinicie o container manualmente.", "warn")


def executar_atualizacao(sid, amb, versao, url_manual=None):
    app_s = amb["srv_app"]
    bd_s  = amb["srv_bd"]
    s     = sessoes[sid]
    modo_auto = s.get("modo_auto", False)

    def falha(msg):
        log(sid, f"❌ {msg}", "erro")
        log(sid, "__FIM_ERRO__", "controle")
        s["finalizado"] = True

    def ok(msg):
        log(sid, f"✅ {msg}", "ok")

    def etapa(n, t):
        log(sid, f"__ETAPA__{n}__{t}", "etapa")

    def cancela():
        log(sid, "Operação cancelada pelo usuário.", "warn")
        s["finalizado"] = True

    try:
        # ── VERIFICAR ARQUIVO NO SERVIDOR ──
        log(sid, "Verificando se o arquivo de atualização já existe no servidor...")
        arquivo_ja_existe = False
        nome_jar = None
        
        try:
            ssh_app_check = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
            _, out_list, code_list = ssh_exec(sid, ssh_app_check, 
                f"find {app_s['esus_dir']} -maxdepth 1 -name 'eSUS-AB-PEC-*.jar' -type f 2>/dev/null | head -1",
                timeout=10
            )
            
            if code_list == 0 and out_list.strip():
                arquivo_path = out_list.strip()
                nome_jar = arquivo_path.split('/')[-1]
                _, out_size, code_size = ssh_exec(sid, ssh_app_check, f"stat -c%s {arquivo_path} 2>/dev/null", timeout=10)
                
                if code_size == 0 and out_size.strip():
                    tamanho_mb = int(out_size.strip()) / (1024 * 1024)
                    if tamanho_mb >= 500:
                        log(sid, f"✅ Arquivo encontrado: {nome_jar} ({tamanho_mb:.1f}M)", "ok")
                        arquivo_ja_existe = True
                    else:
                        log(sid, f"⚠ Arquivo incompleto ({tamanho_mb:.1f}M). Prosseguindo com download...", "warn")
            ssh_app_check.close()
        except Exception as e:
            log(sid, f"Aviso ao verificar arquivo: {e}", "warn")
            
        if not arquivo_ja_existe:
            # ── ETAPA 1: URL ──
            etapa(1, f"Verificar versão {versao} no site do e-SUS")
            if not aguardar(sid): cancela(); return
            url_jar = buscar_url_versao(versao, url_manual, sid=sid)
            if not url_jar:
                falha("URL de download não encontrada.")
                return
            nome_jar = url_jar.split("/")[-1]
            ok(f"Arquivo: {nome_jar}")

            # ── ETAPA 2: Parar serviço ──
            etapa(2, f"Parar serviço no servidor de aplicação ({app_s['host']})")
            if not aguardar(sid): cancela(); return
            try:
                ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
            except Exception as e:
                falha(f"Falha ao conectar: {e}"); return
            ssh_exec(sid, ssh_app, f"systemctl stop {app_s['service']} 2>/dev/null || true", timeout=15)
            ssh_exec(sid, ssh_app, "pkill -9 -f 'java' || true", timeout=10)
            time.sleep(3)
            ok("Serviço parado.")

            # ── ETAPA 3: Backup ──
            etapa(3, f"Backup do banco '{bd_s['db_name']}'")
            if not aguardar(sid): ssh_app.close(); cancela(); return
            data_bkp  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            nome_dump = f"esusbkp{data_bkp}.dump"
            backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backup_dbesus")
            dump_path  = os.path.join(backup_dir, nome_dump)
            os.makedirs(backup_dir, exist_ok=True)

            arquivos_antigos = [
                f for f in os.listdir(backup_dir)
                if f.startswith("esusbkp") and f.endswith(".dump") and f != nome_dump
            ]
            for arq in arquivos_antigos:
                try: os.remove(os.path.join(backup_dir, arq))
                except: pass

            log(sid, "Executando pg_dump via docker exec...")
            import subprocess
            cmd_dump = [
                "docker", "exec", "-e", f"PGPASSWORD={bd_s['senha']}",
                bd_s["container"], "pg_dump", "-U", "postgres", "-Fc", bd_s["db_name"]
            ]
            try:
                with open(dump_path, "wb") as f:
                    result = subprocess.run(cmd_dump, stdout=f, stderr=subprocess.PIPE, timeout=300)
                if result.returncode != 0:
                    falha(f"Erro no backup: {result.stderr.decode().strip()}")
                    ssh_app.close(); return
                ok(f"Backup salvo.")
            except Exception as e:
                falha(f"Erro no backup: {e}")
                ssh_app.close(); return

            # ── ETAPA 4: Download ──
            etapa(4, f"Download do instalador")
            if not aguardar(sid): ssh_app.close(); cancela(); return
            try: ssh_app.close()
            except: pass
            
            try:
                ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
            except Exception as e:
                falha(f"Falha ao reconectar: {e}"); return

            cmd_wget = f"cd {app_s['esus_dir']} && wget -q --show-progress --progress=dot:mega -O '{nome_jar}' '{url_jar}' 2>&1 && echo 'DOWNLOAD_OK' || echo 'DOWNLOAD_FAIL'"
            log(sid, "  Baixando...", "cmd")
            _, out_dl, code_dl = ssh_exec(sid, ssh_app, cmd_wget, timeout=1800, is_download=True)
            if code_dl != 0 or "DOWNLOAD_FAIL" in out_dl:
                falha("Falha no download.")
                ssh_app.close(); return
            ok("Download concluído.")

        else:
            log(sid, "Pulando etapas 1-4 (arquivo já presente). Indo direto para aplicação...", "cmd")
            try:
                ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
            except Exception as e:
                falha(f"Falha ao conectar: {e}"); return

        # ── ETAPA 5: Instalar ──
        senha_bd = bd_s.get("pg_senha") or bd_s["senha"] or ""
        pg_user  = bd_s.get("pg_usuario") or bd_s["usuario"] or "postgres"
        usa_docker_app = bool(app_s.get("usa_docker", 0))

        if usa_docker_app and bd_s.get("db_host_interno"):
            jdbc_host = bd_s["db_host_interno"]
        elif usa_docker_app and bd_s.get("container"):
            jdbc_host = bd_s["container"]
        else:
            jdbc_host = bd_s["host"]
            
        jdbc = f"jdbc:postgresql://{jdbc_host}:5432/{bd_s['db_name']}"
        esus_dir = app_s["esus_dir"]
        
        # Correção vital: Usa o caminho absoluto garantido!
        jar_full = f"{esus_dir}/{nome_jar}"

        etapa(5, f"Aplicar atualização do e-SUS PEC")
        if not aguardar(sid): ssh_app.close(); cancela(); return

        log(sid, "  Garantindo porta livre e preparando ambiente...", "cmd")
        ssh_exec(sid, ssh_app, f"systemctl stop {app_s['service']} 2>/dev/null || true", timeout=15)
        ssh_exec(sid, ssh_app, "pkill -9 -f 'java' 2>/dev/null || true", timeout=15)
        time.sleep(4)
        
        try: ssh_app.close()
        except: pass
        
        reconectado = False
        for _ in range(1, 13): 
            try:
                ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
                reconectado = True
                break
            except:
                time.sleep(5)
                
        if not reconectado:
            falha("Falha ao reconectar."); return
            
        ssh_exec(sid, ssh_app, "apt-get update -qq && apt-get install -y file -qq 2>/dev/null || true", timeout=60)

        log(sid, "  Aplicando patch temporario...", "cmd")
        ssh_exec(sid, ssh_app,
            "cp /bin/ps /bin/ps.real && "
            "printf '#!/bin/bash\nif echo \"$@\" | grep -q \"comm 1\"; then\n  echo systemd\nelse\n  /bin/ps.real \"$@\"\nfi\n' > /bin/ps_wrapper && "
            "chmod +x /bin/ps_wrapper && "
            "mount --bind /bin/ps_wrapper /bin/ps 2>/dev/null || cp /bin/ps_wrapper /bin/ps",
            timeout=15
        )

        # Arquitetura robusta de script igual à do Downgrade
        script_path = f"{esus_dir}/_install.sh"
        import base64 as _b64
        script_content = (
            "#!/bin/bash\n"
            f"cd \"{esus_dir}\"\n"
            f"echo S | /usr/bin/java -jar \"{jar_full}\" -console -backup "
            f"-url=\"{jdbc}\" -username=\"{pg_user}\" -password=\"{senha_bd}\"\n"
        )
        script_b64 = _b64.b64encode(script_content.encode()).decode()
        ssh_exec(sid, ssh_app, f"echo '{script_b64}' | base64 -d > '{script_path}' && chmod +x '{script_path}'", timeout=10)

        log(sid, "Executando instalador (pode levar vários minutos)...")
        _, err, code = ssh_exec(sid, ssh_app, f"bash '{script_path}' 2>&1", timeout=3600)
        
        ssh_exec(sid, ssh_app,
            f"umount /bin/ps 2>/dev/null; cp /bin/ps.real /bin/ps 2>/dev/null || true; rm -f /bin/ps.real /bin/ps_wrapper '{script_path}' 2>/dev/null || true",
            timeout=10
        )

        if code != 0:
            falha("Atualização encerrou com erro.")
            ssh_app.close(); return

        time.sleep(5)
        restart_servico(sid, ssh_app, app_s)
        ok("Atualização concluída!")
        ssh_app.close()
        
        log(sid, "━" * 50)
        log(sid, "__FIM_OK__", "controle")
        s["finalizado"] = True

    except Exception as e:
        falha(f"Erro inesperado: {e}")
        
# ─── Rotas ────────────────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("img/favicon.ico")

@app.route("/login", methods=["GET"])
def login():
    if session.get("usuario_id"):
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    d = request.get_json(silent=True) or {}
    username = d.get("username", "").strip()
    senha    = d.get("senha", "")
    with get_db() as db:
        row = db.execute(
            "SELECT id, username, papel FROM usuarios WHERE username=? AND senha_hash=? AND ativo=1",
            (username, hash_senha(senha))
        ).fetchone()
    if not row:
        return jsonify({"erro": "Usuário ou senha inválidos"}), 401
    session["usuario_id"]   = row["id"]
    session["usuario_nome"] = row["username"]
    session["usuario_papel"] = row["papel"]
    return jsonify({"ok": True, "papel": row["papel"]})

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/auth/me", methods=["GET"])
@login_requerido
def api_me():
    return jsonify({
        "id":    session["usuario_id"],
        "nome":  session["usuario_nome"],
        "papel": session["usuario_papel"],
    })

# ─── Backups manuais ──────────────────────────────────────────────────────────

@app.route("/api/backup/iniciar", methods=["POST"])
@login_requerido
def api_backup_iniciar():
    d = request.get_json(silent=True) or {}
    ambiente_id = d.get("ambiente_id")
    if not ambiente_id:
        return jsonify({"erro": "ambiente_id obrigatório"}), 400

    with get_db() as db:
        amb = row_to_dict(db.execute("SELECT * FROM ambientes WHERE id=?", (ambiente_id,)).fetchone())
        if not amb:
            return jsonify({"erro": "Ambiente não encontrado"}), 404
        bd_s = row_to_dict(db.execute("SELECT * FROM srv_bd WHERE ambiente_id=?", (ambiente_id,)).fetchone())
        if not bd_s:
            return jsonify({"erro": "Ambiente sem servidor de banco cadastrado"}), 400

    sid = str(uuid.uuid4())
    sessions[sid] = {"q": queue.Queue(), "done": False, "confirm": None}

    def executar_backup():
        q = sessions[sid]["q"]

        def log(msg, tipo="info"):
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            q.put({"ts": ts, "msg": msg, "tipo": tipo})

        def ok(msg): log(msg, "ok")

        def etapa(n, titulo):
            q.put({"ts": "", "msg": f"__ETAPA__{n}__{titulo}", "tipo": "etapa"})

        def erro(msg):
            log(msg, "erro")
            q.put({"ts": "", "msg": "__FIM_ERRO__", "tipo": "controle"})
            sessions[sid]["done"] = True

        # ── ETAPA 1: Verificar conexão ──────────────────────────────────────
        etapa(1, "Verificar conexão com o banco")
        log(f"  Ambiente  : {amb['nome']}")
        log(f"  Container : {bd_s['container']}")
        log(f"  Banco     : {bd_s['db_name']}")

        import subprocess
        # Testa se o container responde
        test_cmd = ["docker", "exec", bd_s["container"], "pg_isready", "-U", "postgres"]
        try:
            test = subprocess.run(test_cmd, capture_output=True, timeout=15)
            if test.returncode != 0:
                # pg_isready pode não existir — tenta psql
                test2 = subprocess.run(
                    ["docker", "exec", "-e", f"PGPASSWORD={bd_s['senha']}",
                     bd_s["container"], "psql", "-U", "postgres", "-c", "\\l", "-t"],
                    capture_output=True, timeout=15
                )
                if test2.returncode != 0:
                    erro(f"Não foi possível conectar ao container '{bd_s['container']}'. Verifique se está em execução.")
                    return
            ok("Conexão com o banco verificada.")
        except subprocess.TimeoutExpired:
            erro("Timeout ao verificar conexão.")
            return
        except Exception as e:
            erro(f"Erro ao verificar container: {e}")
            return

        # ── ETAPA 2: pg_dump ────────────────────────────────────────────────
        data_bkp  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_dump = f"bkp_{bd_s['db_name']}_{data_bkp}.dump"
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backup_dbesus")
        dump_path  = os.path.join(backup_dir, nome_dump)

        etapa(2, "Executar pg_dump")
        log(f"  Destino   : {dump_path}")
        os.makedirs(backup_dir, exist_ok=True)

        cmd_dump = [
            "docker", "exec",
            "-e", f"PGPASSWORD={bd_s['senha']}",
            bd_s["container"],
            "pg_dump", "-U", "postgres", "-Fc", bd_s["db_name"]
        ]
        log("Executando pg_dump...", "cmd")
        try:
            with open(dump_path, "wb") as f:
                result = subprocess.run(cmd_dump, stdout=f, stderr=subprocess.PIPE, timeout=300)
            if result.returncode != 0:
                err_msg = result.stderr.decode().strip()
                if os.path.exists(dump_path):
                    os.remove(dump_path)
                erro(f"pg_dump falhou: {err_msg}")
                return
            size_kb = os.path.getsize(dump_path) // 1024
            ok(f"Dump gerado: {nome_dump} ({size_kb} KB)")
        except subprocess.TimeoutExpired:
            erro("Timeout — pg_dump demorou mais de 5 minutos.")
            return
        except Exception as e:
            erro(f"Erro no pg_dump: {e}")
            return

        # ── ETAPA 3: Registrar ──────────────────────────────────────────────
        etapa(3, "Registrar backup")
        try:
            size_kb = os.path.getsize(dump_path) // 1024
            with get_db() as db:
                db.execute(
                    "INSERT INTO backups (ambiente_id, db_name, arquivo, tamanho_kb, status) VALUES (?,?,?,?,?)",
                    (ambiente_id, bd_s["db_name"], nome_dump, size_kb, "ok")
                )
            ok(f"Backup registrado com sucesso. Tamanho final: {size_kb} KB")
            log(f"  Arquivo   : {nome_dump}", "cmd")
        except Exception as e:
            erro(f"Erro ao registrar backup: {e}")
            return

        q.put({"ts": "", "msg": "__FIM_OK__", "tipo": "controle"})
        sessions[sid]["done"] = True

    threading.Thread(target=executar_backup, daemon=True).start()
    return jsonify({"sid": sid})


@app.route("/api/backup/stream/<sid>")
@login_requerido
def api_backup_stream(sid):
    if sid not in sessions:
        return jsonify({"erro": "Sessão inválida"}), 404

    def gerar():
        q = sessions[sid]["q"]
        while True:
            try:
                item = q.get(timeout=25)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("tipo") == "controle" and item.get("msg") in ("__FIM_OK__", "__FIM_ERRO__"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'tipo':'ping','msg':''})}\n\n"

    return Response(gerar(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/backup/historico", methods=["GET"])
@login_requerido
def api_backup_historico():
    ambiente_id = request.args.get("ambiente_id")
    with get_db() as db:
        if ambiente_id:
            rows = db.execute(
                """SELECT b.*, a.nome as ambiente_nome
                   FROM backups b JOIN ambientes a ON a.id=b.ambiente_id
                   WHERE b.ambiente_id=? ORDER BY b.criado DESC LIMIT 50""",
                (ambiente_id,)
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT b.*, a.nome as ambiente_nome
                   FROM backups b JOIN ambientes a ON a.id=b.ambiente_id
                   ORDER BY b.criado DESC LIMIT 100"""
            ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/backup/arquivos", methods=["GET"])
@login_requerido
def api_backup_arquivos():
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backup_dbesus")
    if not os.path.isdir(backup_dir):
        return jsonify([])
    arquivos = []
    for f in sorted(os.listdir(backup_dir), reverse=True):
        if f.endswith(".dump"):
            path = os.path.join(backup_dir, f)
            arquivos.append({
                "arquivo": f,
                "tamanho_kb": os.path.getsize(path) // 1024,
                "modificado": datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
            })
    return jsonify(arquivos)


@app.route("/api/backup/deletar", methods=["POST"])
@login_requerido
def api_backup_deletar():
    if session.get("usuario_papel") != "admin":
        return jsonify({"erro": "Acesso negado"}), 403
    d = request.get_json(silent=True) or {}
    arquivo = d.get("arquivo", "")
    if not arquivo or "/" in arquivo or "\\" in arquivo or not arquivo.endswith(".dump"):
        return jsonify({"erro": "Arquivo inválido"}), 400
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backup_dbesus")
    path = os.path.join(backup_dir, arquivo)
    if os.path.exists(path):
        os.remove(path)
    with get_db() as db:
        db.execute("DELETE FROM backups WHERE arquivo=?", (arquivo,))
    return jsonify({"ok": True})


@app.route("/api/backup/limpar", methods=["POST"])
@login_requerido
def api_backup_limpar():
    """Remove backups locais e registros do banco conforme política escolhida.
    Políticas:
      - manter_ultimos: N   — mantém os N mais recentes por banco, apaga o resto
      - mais_antigos_que: N — apaga arquivos com mais de N dias
      - todos              — apaga tudo (exceto o mais recente de cada banco)
    """
    if session.get("usuario_papel") != "admin":
        return jsonify({"erro": "Acesso negado"}), 403

    d       = request.get_json(silent=True) or {}
    politica = d.get("politica", "manter_ultimos")   # manter_ultimos | mais_antigos_que | todos
    valor    = int(d.get("valor", 3))                 # N para manter ou dias

    backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backup_dbesus")
    if not os.path.isdir(backup_dir):
        return jsonify({"removidos": [], "erros": []})

    # Lista todos os .dump com metadados
    arquivos = []
    for nome in os.listdir(backup_dir):
        if not nome.endswith(".dump"):
            continue
        path = os.path.join(backup_dir, nome)
        try:
            mtime = os.path.getmtime(path)
            size  = os.path.getsize(path)
            arquivos.append({"nome": nome, "path": path, "mtime": mtime, "size": size})
        except Exception:
            pass

    arquivos.sort(key=lambda x: x["mtime"], reverse=True)  # mais recente primeiro

    agora = datetime.datetime.now().timestamp()
    para_apagar = []

    if politica == "manter_ultimos":
        # Agrupa por banco (prefixo antes do timestamp) e mantém os N mais recentes
        from collections import defaultdict
        grupos = defaultdict(list)
        for arq in arquivos:
            # Extrai prefixo: tudo antes do primeiro dígito de data (YYYYMMDD)
            import re as _re
            m = _re.match(r'^([a-zA-Z_-]+)', arq["nome"])
            prefixo = m.group(1) if m else arq["nome"]
            grupos[prefixo].append(arq)
        for prefixo, lista in grupos.items():
            lista.sort(key=lambda x: x["mtime"], reverse=True)
            para_apagar.extend(lista[valor:])  # apaga além dos N mais recentes

    elif politica == "mais_antigos_que":
        limite = agora - (valor * 86400)  # valor em dias
        para_apagar = [a for a in arquivos if a["mtime"] < limite]

    elif politica == "todos":
        # Mantém apenas o mais recente de cada banco
        from collections import defaultdict
        import re as _re
        grupos = defaultdict(list)
        for arq in arquivos:
            m = _re.match(r'^([a-zA-Z_-]+)', arq["nome"])
            prefixo = m.group(1) if m else arq["nome"]
            grupos[prefixo].append(arq)
        for prefixo, lista in grupos.items():
            lista.sort(key=lambda x: x["mtime"], reverse=True)
            para_apagar.extend(lista[1:])  # mantém só o mais recente

    removidos = []
    erros     = []
    with get_db() as db:
        for arq in para_apagar:
            try:
                os.remove(arq["path"])
                db.execute("DELETE FROM backups WHERE arquivo=?", (arq["nome"],))
                removidos.append({"arquivo": arq["nome"], "tamanho_kb": arq["size"] // 1024})
            except Exception as e:
                erros.append({"arquivo": arq["nome"], "erro": str(e)})

    total_kb = sum(r["tamanho_kb"] for r in removidos)
    return jsonify({"removidos": removidos, "erros": erros, "total_kb": total_kb})


# ─── Usuários ─────────────────────────────────────────────────────────────────

@app.route("/api/usuarios", methods=["GET"])
@login_requerido
def api_listar_usuarios():
    if session.get("usuario_papel") != "admin":
        return jsonify({"erro": "Acesso negado"}), 403
    with get_db() as db:
        rows = db.execute(
            "SELECT id, username, papel, criado, ativo FROM usuarios ORDER BY criado DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/usuarios", methods=["POST"])
@login_requerido
def api_criar_usuario():
    if session.get("usuario_papel") != "admin":
        return jsonify({"erro": "Acesso negado"}), 403
    d = request.get_json(silent=True) or {}
    username = d.get("username", "").strip()
    senha    = d.get("senha", "")
    papel    = d.get("papel", "operador")
    if not username or not senha:
        return jsonify({"erro": "Usuário e senha obrigatórios"}), 400
    if papel not in ("admin",):
        papel = "admin"  # único perfil suportado
    try:
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO usuarios (username, senha_hash, papel) VALUES (?,?,?)",
                (username, hash_senha(senha), papel)
            )
        return jsonify({"id": cur.lastrowid}), 201
    except sqlite3.IntegrityError:
        return jsonify({"erro": "Usuário já existe"}), 409

@app.route("/api/usuarios/<int:uid>", methods=["PUT"])
@login_requerido
def api_editar_usuario(uid):
    if session.get("usuario_papel") != "admin":
        return jsonify({"erro": "Acesso negado"}), 403
    d = request.get_json(silent=True) or {}
    with get_db() as db:
        row = db.execute("SELECT id FROM usuarios WHERE id=?", (uid,)).fetchone()
        if not row:
            return jsonify({"erro": "Usuário não encontrado"}), 404
        if "senha" in d and d["senha"]:
            db.execute("UPDATE usuarios SET senha_hash=? WHERE id=?", (hash_senha(d["senha"]), uid))
        if "papel" in d:
            db.execute("UPDATE usuarios SET papel=? WHERE id=?", (d["papel"], uid))
        if "ativo" in d:
            db.execute("UPDATE usuarios SET ativo=? WHERE id=?", (1 if d["ativo"] else 0, uid))
    return jsonify({"ok": True})

@app.route("/api/usuarios/<int:uid>", methods=["DELETE"])
@login_requerido
def api_deletar_usuario(uid):
    if session.get("usuario_papel") != "admin":
        return jsonify({"erro": "Acesso negado"}), 403
    if uid == session.get("usuario_id"):
        return jsonify({"erro": "Não é possível deletar o próprio usuário"}), 400
    with get_db() as db:
        db.execute("DELETE FROM usuarios WHERE id=?", (uid,))
    return jsonify({"ok": True})

@app.route("/")
@login_requerido
def index():
    return render_template("index.html")

@app.route("/api/ambientes", methods=["GET"])
@login_requerido
def api_ambientes():
    with get_db() as db:
        rows = db.execute("""
            SELECT a.*, sa.host as app_host, sa.porta as app_porta, sa.usuario as app_usuario,
                   sb.host as bd_host, sb.porta as bd_porta, sb.usuario as bd_usuario, sb.db_name
            FROM ambientes a
            LEFT JOIN srv_app sa ON a.id = sa.ambiente_id
            LEFT JOIN srv_bd sb ON a.id = sb.ambiente_id
            ORDER BY a.nome
        """).fetchall()
    return jsonify([row_to_dict(r) for r in rows])

@app.route("/api/ambientes/<int:aid>", methods=["GET"])
@login_requerido
def api_ambiente(aid):
    amb = get_ambiente_full(aid)
    if not amb:
        return jsonify({"erro": "Ambiente não encontrado"}), 404
    return jsonify(amb)

@app.route("/api/ambientes", methods=["POST"])
@login_requerido
def api_criar_ambiente():
    d = request.get_json(silent=True) or {}
    if not d.get("nome"):
        return jsonify({"erro": "Nome obrigatório"}), 400
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO ambientes (nome, tipo) VALUES (?, ?)",
            (d["nome"], d.get("tipo", "homologacao"))
        )
        aid = cur.lastrowid
        db.execute(
            "INSERT INTO srv_app (ambiente_id, host, porta, usuario, senha, esus_dir, service, usa_docker, container) VALUES (?,?,?,?,?,?,?,?,?)",
            (aid, d.get("app_host", "localhost"), d.get("app_porta", 22), d.get("app_usuario", "root"),
             d.get("app_senha", ""), d.get("app_dir", "/opt/e-SUS"), d.get("app_service", "e-SUS-PEC"),
             1 if d.get("app_docker") else 0, d.get("app_container", "e-SUS-PEC"))
        )
        db.execute(
            "INSERT INTO srv_bd (ambiente_id, host, porta, usuario, senha, db_name, container, db_host_interno, pg_usuario, pg_senha) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, d.get("bd_host", "localhost"), d.get("bd_porta", 22), d.get("bd_usuario", "root"),
             d.get("bd_senha", ""), d.get("bd_nome", "novosgar"), d.get("bd_container", "postgresql-db-1"),
             d.get("bd_host_interno", ""), d.get("pg_usuario", "postgres"), d.get("pg_senha", ""))
        )
    return jsonify({"id": aid}), 201

@app.route("/api/ambientes/<int:aid>", methods=["PUT"])
@login_requerido
def api_editar_ambiente(aid):
    d = request.get_json(silent=True) or {}
    with get_db() as db:
        db.execute("UPDATE ambientes SET nome=?, tipo=? WHERE id=?", (d.get("nome"), d.get("tipo", "homologacao"), aid))
        db.execute("""
            UPDATE srv_app SET host=?, porta=?, usuario=?, senha=?, esus_dir=?, service=?, usa_docker=?, container=?
            WHERE ambiente_id=?
        """, (d.get("app_host", "localhost"), d.get("app_porta", 22), d.get("app_usuario", "root"),
              d.get("app_senha", ""), d.get("app_dir", "/opt/e-SUS"), d.get("app_service", "e-SUS-PEC"),
              1 if d.get("app_docker") else 0, d.get("app_container", "e-SUS-PEC"), aid))
        db.execute("""
            UPDATE srv_bd SET host=?, porta=?, usuario=?, senha=?, db_name=?, container=?, db_host_interno=?, pg_usuario=?, pg_senha=?
            WHERE ambiente_id=?
        """, (d.get("bd_host", "localhost"), d.get("bd_porta", 22), d.get("bd_usuario", "root"),
              d.get("bd_senha", ""), d.get("bd_nome", "novosgar"), d.get("bd_container", "postgresql-db-1"),
              d.get("bd_host_interno", ""), d.get("pg_usuario", "postgres"), d.get("pg_senha", ""), aid))
    return jsonify({"ok": True})

@app.route("/api/ambientes/<int:aid>", methods=["DELETE"])
@login_requerido
def api_deletar_ambiente(aid):
    with get_db() as db:
        db.execute("DELETE FROM ambientes WHERE id=?", (aid,))
    return jsonify({"ok": True})

@app.route("/api/versoes", methods=["GET"])
@login_requerido
def api_versoes():
    return jsonify(buscar_versoes_disponiveis())

@app.route("/api/versoes/sync", methods=["POST"])
@login_requerido
def api_sync_versoes():
    """Dispara sincronização manual de versões."""
    sincronizar_versoes(forcar=True)
    return jsonify({"ok": True, "msg": "Sincronização iniciada em background."})

@app.route("/api/versoes/status", methods=["GET"])
@login_requerido
def api_sync_status():
    """Retorna status da última sincronização incluindo debug."""
    return jsonify(_sync_status)

@app.route("/api/versoes/testar", methods=["POST"])
@login_requerido
def api_testar_url():
    """Testa se a URL de uma versão está acessível."""
    versao = request.json.get("versao")
    with get_db() as db:
        row = db.execute("SELECT url_linux FROM versoes WHERE versao=?", (versao,)).fetchone()
    if not row or not row["url_linux"]:
        return jsonify({"ok": False, "erro": "URL não encontrada"})
    url = row["url_linux"]
    try:
        r = requests.get(url, headers={"Range": "bytes=0-0", "User-Agent": "wget/1.21"},
                        verify=False, timeout=15, stream=True)
        r.close()
        ok = r.status_code in (200, 206)
        return jsonify({"ok": ok, "status": r.status_code, "url": url,
                       "tamanho": r.headers.get("Content-Range", r.headers.get("Content-Length","?"))})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e), "url": url})

@app.route("/api/versoes/<versao>", methods=["DELETE"])
@login_requerido
def api_deletar_versao(versao):
    """Remove uma versão do banco para forçar redescoberta."""
    with get_db() as db:
        db.execute("DELETE FROM versoes WHERE versao=?", (versao,))
    return jsonify({"ok": True})

@app.route("/api/resolver-url", methods=["POST"])
@login_requerido
def api_resolver_url():
    d = request.get_json(silent=True) or {}
    versao = d.get("versao")
    url_manual = d.get("url_manual")
    
    if not versao:
        return jsonify({"ok": False, "erro": "Versão obrigatória"}), 400
    
    url = buscar_url_versao(versao, url_manual)
    if not url:
        return jsonify({"ok": False, "erro": f"URL não encontrada para versão {versao}"}), 404
    
    return jsonify({"ok": True, "url": url})

@app.route("/api/iniciar", methods=["POST"])
@login_requerido
def api_iniciar():
    d = request.get_json(silent=True) or {}
    aid = d.get("ambiente_id")
    versao = d.get("versao")
    url_download = d.get("url_download")
    
    # Lendo a instrução do frontend (padrão é False se não for enviado)
    modo_auto = d.get("modo_auto", False)
    
    if not aid or not versao:
        return jsonify({"erro": "ambiente_id e versao obrigatórios"}), 400
    
    amb = get_ambiente_full(aid)
    if not amb:
        return jsonify({"erro": "Ambiente não encontrado"}), 404
    
    # Agora a sessão é criada obedecendo a escolha do usuário
    sid = nova_sessao(modo_auto=modo_auto)
    
    threading.Thread(
        target=executar_atualizacao,
        args=(sid, amb, versao, url_download),
        daemon=True
    ).start()
    
    return jsonify({"sid": sid}), 202

@app.route("/api/stream/<sid>")
@login_requerido
def api_stream(sid):
    if sid not in sessoes:
        return "Sessão não encontrada", 404
    
    def gerar():
        while True:
            try:
                msg = sessoes[sid]["log_queue"].get(timeout=1)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                if sessoes[sid]["finalizado"]:
                    break
                yield ": keepalive\n\n"
    
    return Response(gerar(), mimetype="text/event-stream")

@app.route("/api/confirmar/<sid>", methods=["POST"])
@login_requerido
def api_confirmar(sid):
    if sid not in sessoes:
        return jsonify({"erro": "Sessão não encontrada"}), 404
    
    d = request.get_json(silent=True) or {}
    if d.get("cancelar"):
        sessoes[sid]["cancelado"] = True
    else:
        sessoes[sid]["aguardando"].set()
    
    return jsonify({"ok": True})


# ─── Downgrade ────────────────────────────────────────────────────────────────

def executar_downgrade(sid, amb, versao, url_download):
    app_s = amb["srv_app"]
    bd_s  = amb["srv_bd"]

    def falha(msg):
        log(sid, f"❌ {msg}", "erro")
        log(sid, "__FIM_ERRO__", "controle")
        sessoes[sid]["finalizado"] = True

    def ok(msg):
        log(sid, f"✅ {msg}", "ok")

    def etapa(n, t):
        log(sid, f"__ETAPA__{n}__{t}", "etapa")

    def cancela():
        log(sid, "Operação cancelada pelo usuário.", "warn")
        sessoes[sid]["finalizado"] = True

    try:
        # ── ETAPA 1: Parar serviço ─────────────────────────────────────────
        etapa(1, f"Parar serviço no servidor ({app_s['host']})")
        log(sid, f"  Host    : {app_s['host']}:{app_s['porta']}")
        log(sid, f"  Serviço : {app_s['service']}")
        log(sid, "  ⚠ O sistema ficará indisponível durante o downgrade.", "warn")
        if not aguardar(sid):
            cancela()
            return

        try:
            ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
        except Exception as e:
            falha(f"Falha ao conectar no servidor: {e}")
            return
        ok("Conectado.")

        _, _, c1 = ssh_exec(sid, ssh_app, f"systemctl stop {app_s['service']}", timeout=15)
        if c1 != 0:
            _, _, c2 = ssh_exec(sid, ssh_app, f"service {app_s['service']} stop", timeout=15)
            if c2 != 0:
                ssh_exec(sid, ssh_app, "pkill -9 -f 'java' || true", timeout=10)
                time.sleep(3)
        ok("Serviço parado.")

        # ── ETAPA 2: Backup do banco ───────────────────────────────────────
        data_bkp  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_dump = f"downgrade_bkp_{data_bkp}.dump"
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backup_dbesus")
        dump_path  = os.path.join(backup_dir, nome_dump)

        etapa(2, f"Backup de segurança do banco '{bd_s['db_name']}'")
        log(sid, f"  Container : {bd_s['container']}")
        log(sid, f"  Banco     : {bd_s['db_name']}")
        log(sid, f"  Destino   : {dump_path}")
        log(sid, "  ⚠ Use este backup para reverter se necessário.", "warn")
        if not aguardar(sid):
            ssh_app.close()
            cancela()
            return

        os.makedirs(backup_dir, exist_ok=True)
        import subprocess as _sp
        cmd_dump = [
            "docker", "exec",
            "-e", f"PGPASSWORD={bd_s['senha']}",
            bd_s["container"],
            "pg_dump", "-U", "postgres", "-Fc", bd_s["db_name"]
        ]
        try:
            with open(dump_path, "wb") as f:
                result = _sp.run(cmd_dump, stdout=f, stderr=_sp.PIPE, timeout=300)
            if result.returncode != 0:
                falha(f"Backup falhou: {result.stderr.decode().strip()}")
                ssh_app.close()
                return
            size_kb = os.path.getsize(dump_path) // 1024
            ok(f"Backup salvo: {nome_dump} ({size_kb} KB)")
            with get_db() as db:
                db.execute(
                    "INSERT INTO backups (ambiente_id, db_name, arquivo, tamanho_kb, status) VALUES (?,?,?,?,?)",
                    (amb["id"], bd_s["db_name"], nome_dump, size_kb, "ok")
                )
        except _sp.TimeoutExpired:
            falha("Timeout no backup — pg_dump demorou mais de 5 minutos.")
            ssh_app.close()
            return
        except Exception as e:
            falha(f"Erro no backup: {e}")
            ssh_app.close()
            return

        # Reconecta SSH após o backup (pg_dump local pode ter deixado a sessão idle)
        try:
            ssh_app.close()
        except Exception:
            pass
        try:
            log(sid, "  Reconectando SSH após backup...", "cmd")
            ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
            ok("SSH reconectado.")
        except Exception as e:
            falha(f"Falha ao reconectar SSH: {e}")
            return

        # ── ETAPA 3: Desinstalador nativo ──────────────────────────────────
        etapa(3, "Executar desinstalador nativo (limpar travas de versão)")
        log(sid, f"  Diretório : {app_s['esus_dir']}")
        log(sid, "  O desinstalador remove binários e registros do Java.")
        log(sid, "  ⚠ O banco de dados NÃO será apagado.", "warn")

        if not aguardar(sid):
            ssh_app.close()
            cancela()
            return

        container_app  = app_s.get("container", "").strip()
        usa_docker_app = bool(app_s.get("usa_docker", 0))

        # Busca o desinstalador — tenta no container se usa_docker, senão direto via SSH
        # SSH já está dentro do container (ou host direto) — executa sem docker exec
        def run_cmd(cmd, timeout=30):
            return ssh_exec(sid, ssh_app, cmd, timeout=timeout)

        def find_file(pattern, maxdepth=2):
            """Encontra arquivo no servidor sem logar o resultado no SSE."""
            find_cmd = "find '{}' -maxdepth {} -name '{}' -type f 2>/dev/null | head -1".format(
                app_s["esus_dir"], maxdepth, pattern
            )
            if usa_docker_app:
                raw_cmd = "docker exec '{}' {}".format(container_app, find_cmd)
            else:
                raw_cmd = find_cmd
            _, stdout, stderr, rc = _ssh_exec_raw(ssh_app, raw_cmd, timeout=10)
            return stdout.strip() if rc == 0 else ""

        # Diagnóstico — lista o que tem na pasta para confirmar o nome real
        _, ls_out, _, _ = _ssh_exec_raw(ssh_app,
            "docker exec '{}' ls -1 '{}' 2>/dev/null || ls -1 '{}' 2>/dev/null".format(
                container_app, app_s["esus_dir"], app_s["esus_dir"]),
            timeout=10
        )
        log(sid, "  Arquivos na pasta: {}".format(", ".join(
            [f for f in ls_out.strip().splitlines() if f.strip()][:10]
        )), "cmd")

        # Verifica existência pelo path direto (mais confiável que find dentro de container)
        # SSH conecta diretamente no container (ou host) — test -f direto, sem docker exec
        desinst_path = "{}/desinstalador.jar".format(app_s["esus_dir"])
        chk_cmd = "test -f '{}' && echo EXISTS || echo MISSING".format(desinst_path)
        _, chk_out, chk_err, chk_rc = _ssh_exec_raw(ssh_app, chk_cmd, timeout=10)
        log(sid, "  Verificando: {} → {}".format(desinst_path, chk_out.strip() or chk_err.strip()[:40]), "cmd")
        desinst_jar = desinst_path if "EXISTS" in chk_out else None
        log(sid, "  Desinstalador detectado: {}".format(desinst_jar or "nenhum"), "cmd")

        if desinst_jar:
            log(sid, f"  Desinstalador encontrado: {desinst_jar}", "cmd")
            # Aplica o mesmo patch de /bin/ps para o desinstalador (também verifica systemd)
            ssh_exec(sid, ssh_app,
                "cp /bin/ps /bin/ps.real 2>/dev/null || true && "
                "printf '#!/bin/bash\nif echo \"$@\" | grep -q \"comm 1\"; then\n  echo systemd\nelse\n  /bin/ps.real \"$@\"\nfi\n' > /bin/ps_wrapper && "
                "chmod +x /bin/ps_wrapper && "
                "mount --bind /bin/ps_wrapper /bin/ps 2>/dev/null || cp /bin/ps_wrapper /bin/ps",
                timeout=10
            )
            cmd_desinst = "cd '{}' && printf 'y\ny\ny\n' | /usr/bin/java -jar '{}' -console 2>&1 || true".format(
                app_s["esus_dir"], desinst_jar
            )
            _, _, code_d = run_cmd(cmd_desinst, timeout=300)
            # Restaura ps
            ssh_exec(sid, ssh_app,
                "umount /bin/ps 2>/dev/null; cp /bin/ps.real /bin/ps 2>/dev/null || true; rm -f /bin/ps.real /bin/ps_wrapper 2>/dev/null || true",
                timeout=10
            )
            if code_d != 0:
                log(sid, "  Desinstalador encerrou com aviso (normal em alguns casos).", "warn")
            else:
                ok("Desinstalador concluído.")
        else:
            log(sid, "  Desinstalador não encontrado. Removendo JARs manualmente...", "warn")
            run_cmd(
                "find '{}' -maxdepth 1 -name 'eSUS-AB-PEC-*.jar' -delete 2>/dev/null || true".format(app_s["esus_dir"]),
                timeout=15
            )

        log(sid, "  Limpando registros de inventário do Java...", "cmd")
        ssh_exec(sid, ssh_app,
            "find /root /home -name '.com.zerog.registry.xml' -delete 2>/dev/null || true; "
            "find /var -name '.com.zerog.registry.xml' -delete 2>/dev/null || true",
            timeout=15
        )
        ok("Travas de versão removidas.")

        # ── ETAPA 4: Download do JAR ───────────────────────────────────────
        nome_jar = url_download.split("/")[-1]
        jar_path = f"{app_s['esus_dir']}/{nome_jar}"

        etapa(4, f"Download do instalador da versão {versao}")
        log(sid, f"  URL     : {url_download}")
        log(sid, f"  Destino : {jar_path}")

        # Verifica se o arquivo já existe — tenta no host e dentro do container
        log(sid, "  Verificando se o arquivo já existe no servidor...")

        # Primeiro descobre se o e-SUS roda em container para saber onde verificar
        _, pid1_check, _ = ssh_exec(sid, ssh_app,
            "ps --no-headers -o comm 1 2>/dev/null || echo unknown", timeout=5)
        pid1_check = pid1_check.strip().lower()
        em_container_check = pid1_check not in ("systemd", "init", "sbin/init")

        arquivo_ja_existe = False
        tamanho_encontrado = None

        if em_container_check:
            # Descobre o container e verifica o arquivo dentro dele
            _, containers_out, _ = ssh_exec(sid, ssh_app,
                "docker ps --format '{{.Names}}' 2>/dev/null | head -5", timeout=10)
            container_check = None
            for cname in containers_out.strip().splitlines():
                if cname.strip():
                    container_check = cname.strip()
                    break
            if container_check:
                _, out_check, _ = ssh_exec(sid, ssh_app,
                    f"docker exec '{container_check}' stat -c%s '{jar_path}' 2>/dev/null || echo 'NOT_FOUND'",
                    timeout=10
                )
                out_check = out_check.strip()
                log(sid, f"  Verificando dentro do container '{container_check}': {out_check[:40]}")
            else:
                out_check = "NOT_FOUND"
        else:
            _, out_check, _ = ssh_exec(sid, ssh_app,
                f"stat -c%s '{jar_path}' 2>/dev/null || echo 'NOT_FOUND'",
                timeout=10
            )
            out_check = out_check.strip()

        if out_check not in ("", "NOT_FOUND"):
            try:
                tamanho_mb = int(out_check) / (1024 * 1024)
                tamanho_encontrado = tamanho_mb
                if tamanho_mb >= 500:
                    log(sid, f"  ✅ Arquivo já existe: {nome_jar} ({tamanho_mb:.1f} MB) — pulando download.", "ok")
                    arquivo_ja_existe = True
                else:
                    log(sid, f"  ⚠ Arquivo incompleto ({tamanho_mb:.1f} MB). Baixando novamente...", "warn")
            except ValueError:
                pass

        if not arquivo_ja_existe:
            if not aguardar(sid):
                ssh_app.close()
                cancela()
                return

            try:
                transport = ssh_app.get_transport()
                if transport is None or not transport.is_active():
                    raise Exception("SSH inativo")
            except Exception:
                log(sid, "Reconectando SSH...", "warn")
                try:
                    ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
                    ok("Reconectado.")
                except Exception as e:
                    falha(f"Falha ao reconectar: {e}")
                    return

            cmd_wget = (
                f"cd {app_s['esus_dir']} && "
                f"wget -q --show-progress --progress=dot:mega -O '{nome_jar}' '{url_download}' 2>&1 && "
                f"echo 'DOWNLOAD_OK' || echo 'DOWNLOAD_FAIL'"
            )
            log(sid, "  Baixando... (pode levar alguns minutos)", "cmd")
            _, out_dl, code_dl = ssh_exec(sid, ssh_app, cmd_wget, timeout=1800, is_download=True)
            if code_dl != 0 or "DOWNLOAD_FAIL" in out_dl:
                falha("Falha no download. Verifique a URL e a conectividade do servidor.")
                ssh_app.close()
                return
            ok(f"Download concluído: {nome_jar}")
        else:
            # Arquivo existe — confirma antes de prosseguir para instalação
            if not aguardar(sid):
                ssh_app.close()
                cancela()
                return

        # ── ETAPA 5: Instalar versão ───────────────────────────────────────
        senha_bd = bd_s.get("pg_senha") or bd_s["senha"] or ""
        pg_user  = bd_s.get("pg_usuario") or bd_s["usuario"] or "postgres"
        if usa_docker_app and bd_s.get("db_host_interno"):
            jdbc_host = bd_s["db_host_interno"]
            log(sid, "  Modo Docker: usando hostname interno '{}' para o banco.".format(jdbc_host), "cmd")
        elif usa_docker_app and bd_s.get("container"):
            jdbc_host = bd_s["container"]
            log(sid, "  Modo Docker: usando container '{}' como host do banco.".format(jdbc_host), "cmd")
        else:
            jdbc_host = bd_s["host"]
        jdbc     = "jdbc:postgresql://{}:5432/{}".format(jdbc_host, bd_s["db_name"])
        esus_dir = app_s["esus_dir"]
        jar_full = "{}/{}".format(esus_dir, nome_jar)

        etapa(5, "Instalar versão {}".format(versao))
        log(sid, "  Pode levar varios minutos.", "warn")
        if not aguardar(sid):
            ssh_app.close()
            cancela()
            return

        # === 1. MATAR PROCESSOS E AGUARDAR CONTAINER ===
        log(sid, "  Garantindo porta 8080 livre...", "cmd")
        ssh_exec(sid, ssh_app, "systemctl stop {} 2>/dev/null || true".format(app_s['service']), timeout=15)
        ssh_exec(sid, ssh_app, "pkill -9 -f 'java' 2>/dev/null || true", timeout=15)
        
        log(sid, "  Reconectando SSH (aguardando caso o container tenha reiniciado)...", "cmd")
        try:
            ssh_app.close()
        except:
            pass
        
        reconectado = False
        # Faz um loop de tentativas (aguarda até 60 segundos pro container voltar)
        for tentativa in range(1, 13): 
            try:
                ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
                reconectado = True
                break
            except Exception:
                time.sleep(5)
                
        if not reconectado:
            falha("Falha ao reconectar. O container/servidor demorou muito para voltar.")
            return
        
        log(sid, "  Conexão estabilizada.", "cmd")

        # === 2. PREPARAR DEPENDÊNCIAS ===
        log(sid, "  Preparando dependências do sistema...", "cmd")
        ssh_exec(sid, ssh_app, "apt-get update -qq && apt-get install -y file -qq 2>/dev/null || true", timeout=60)

        # === 3. APLICAR PATCH DO PS ===
        log(sid, "  Aplicando patch temporario em /bin/ps...", "cmd")
        _, _, rc_patch = ssh_exec(sid, ssh_app,
            "cp /bin/ps /bin/ps.real && "
            "printf '#!/bin/bash\nif echo \"$@\" | grep -q \"comm 1\"; then\n  echo systemd\nelse\n  /bin/ps.real \"$@\"\nfi\n' > /bin/ps_wrapper && "
            "chmod +x /bin/ps_wrapper && "
            "mount --bind /bin/ps_wrapper /bin/ps 2>/dev/null || cp /bin/ps_wrapper /bin/ps",
            timeout=15
        )

        # === 4. CRIAR SCRIPT DE INSTALAÇÃO ===
        script_path = "{}/_install.sh".format(esus_dir)
        import base64 as _b64
        script_lines = [
            "#!/bin/bash",
            'cd "{}"'.format(esus_dir),
            'echo S | /usr/bin/java -jar "{}" -console -url="{}" -username="{}" -password="{}"'.format(
                jar_full, jdbc, pg_user, senha_bd
            ),
        ]
        script_content = "\n".join(script_lines) + "\n"
        script_b64 = _b64.b64encode(script_content.encode()).decode()
        ssh_exec(sid, ssh_app,
            "echo '{}' | base64 -d > '{}' && chmod +x '{}'".format(
                script_b64, script_path, script_path),
            timeout=10
        )
        
        log(sid, "  JDBC URL  : {}".format(jdbc), "cmd")
        log(sid, "  PG User   : {}".format(pg_user), "cmd")

        # === 5. EXECUTAR INSTALADOR ===
        log(sid, "  Executando instalador...")
        _, inst_out, code_inst = ssh_exec(sid, ssh_app, "bash '{}' 2>&1".format(script_path), timeout=3600)
        
        for line in inst_out.splitlines()[-5:] if inst_out else []:
            if any(k in line for k in ["FATAL", "ERROR", "Exception", "refused", "password", "JDBC", "url", "username"]):
                log(sid, "  [diag] {}".format(line.strip()[:120]), "warn")

        # === 6. RESTAURAR PATCH ===
        log(sid, "  Restaurando /bin/ps...", "cmd")
        ssh_exec(sid, ssh_app,
            "umount /bin/ps 2>/dev/null; "
            "cp /bin/ps.real /bin/ps 2>/dev/null || true; "
            "rm -f /bin/ps.real /bin/ps_wrapper '{}' 2>/dev/null || true".format(script_path),
            timeout=10
        )

        if code_inst != 0:
            log(sid, "Instalador encerrou com erro. Veja o log.", "warn")
            falha("Instalacao da versao alvo falhou.")
            ssh_app.close()
            return

        restart_servico(sid, ssh_app, app_s, container_conhecido=container_app if usa_docker_app else None)
        ok(f"Downgrade para versão {versao} concluído!")
        ssh_app.close()
        log(sid, "━" * 50)
        log(sid, f"Ambiente '{amb['nome']}' voltou para versão {versao}!", "ok")
        log(sid, "━" * 50)
        log(sid, "__FIM_OK__", "controle")
        sessoes[sid]["finalizado"] = True

    except Exception as e:
        log(sid, f"Erro inesperado: {e}", "erro")
        log(sid, "__FIM_ERRO__", "controle")
        sessoes[sid]["finalizado"] = True


@app.route("/api/downgrade/iniciar", methods=["POST"])
@login_requerido
def api_downgrade_iniciar():
    d = request.get_json(silent=True) or {}
    ambiente_id  = d.get("ambiente_id")
    versao       = d.get("versao", "").strip()
    url_download = d.get("url_download", "").strip()
    modo_auto    = d.get("modo_auto", False)

    if not ambiente_id or not versao or not url_download:
        return jsonify({"erro": "ambiente_id, versao e url_download são obrigatórios"}), 400

    with get_db() as db:
        amb_row = db.execute("SELECT * FROM ambientes WHERE id=?", (ambiente_id,)).fetchone()
        if not amb_row:
            return jsonify({"erro": "Ambiente não encontrado"}), 404
        amb = row_to_dict(amb_row)
        amb["srv_app"] = row_to_dict(db.execute("SELECT * FROM srv_app WHERE ambiente_id=?", (ambiente_id,)).fetchone())
        amb["srv_bd"]  = row_to_dict(db.execute("SELECT * FROM srv_bd  WHERE ambiente_id=?", (ambiente_id,)).fetchone())

    sid = nova_sessao(modo_auto)
    threading.Thread(target=executar_downgrade, args=(sid, amb, versao, url_download), daemon=True).start()
    return jsonify({"sid": sid})


@app.route("/api/downgrade/stream/<sid>")
@login_requerido
def api_downgrade_stream(sid):
    if sid not in sessoes:
        return "Sessão não encontrada", 404
    def gerar():
        while True:
            try:
                msg = sessoes[sid]["log_queue"].get(timeout=1)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                if sessoes[sid]["finalizado"]:
                    break
                yield ": keepalive\n\n"
    return Response(gerar(), mimetype="text/event-stream")


@app.route("/api/downgrade/confirmar/<sid>", methods=["POST"])
@login_requerido
def api_downgrade_confirmar(sid):
    if sid not in sessoes:
        return jsonify({"erro": "Sessão não encontrada"}), 404
    d = request.get_json(silent=True) or {}
    if d.get("cancelar"):
        sessoes[sid]["cancelado"] = True
    sessoes[sid]["aguardando"].set()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)