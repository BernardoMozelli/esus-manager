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
            usa_docker  INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS srv_bd (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ambiente_id INTEGER NOT NULL REFERENCES ambientes(id) ON DELETE CASCADE,
            host        TEXT NOT NULL,
            porta       INTEGER NOT NULL DEFAULT 22,
            usuario     TEXT NOT NULL,
            senha       TEXT NOT NULL DEFAULT '',
            db_name     TEXT NOT NULL,
            container   TEXT NOT NULL DEFAULT 'postgresql-db-1'
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
            INSERT INTO srv_app (ambiente_id, host, porta, usuario, senha, esus_dir, service, usa_docker)
            VALUES (?,?,?,?,?,?,?,?)
        """, (aid, "localhost", 22, "root", "", "/opt/e-SUS", "e-SUS-PEC", 1))
        db.execute("""
            INSERT INTO srv_bd (ambiente_id, host, porta, usuario, senha, db_name, container)
            VALUES (?,?,?,?,?,?,?)
        """, (aid, "localhost", 22, "root", "rootpassword", "novosgar", "esus_dblocal"))

init_db()
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
    No modo automático, confirma imediatamente sem esperar o usuário.
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
        # ── VALIDAÇÃO INICIAL: Verificar se arquivo já existe no servidor ──────
        log(sid, "Verificando se o arquivo de atualização já existe no servidor...")
        arquivo_existe = False
        nome_jar_existente = None
        
        try:
            ssh_app_check = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
            # Comando simples: verifica se existe arquivo .jar
            _, out_list, code_list = ssh_exec(sid, ssh_app_check, 
                f"find {app_s['esus_dir']} -maxdepth 1 -name 'eSUS-AB-PEC-*.jar' -type f 2>/dev/null | head -1",
                timeout=10
            )
            
            if code_list == 0 and out_list.strip():
                arquivo_path = out_list.strip()
                nome_jar_existente = arquivo_path.split('/')[-1]
                
                # Verifica o tamanho
                _, out_size, code_size = ssh_exec(sid, ssh_app_check,
                    f"stat -c%s {arquivo_path} 2>/dev/null",
                    timeout=10
                )
                
                if code_size == 0 and out_size.strip():
                    try:
                        tamanho_bytes = int(out_size.strip())
                        tamanho_mb = tamanho_bytes / (1024 * 1024)
                        tamanho_str = f"{tamanho_mb:.1f}M"
                        
                        if tamanho_mb >= 500:
                            log(sid, f"✅ Arquivo encontrado: {nome_jar_existente} ({tamanho_str})", "ok")
                            log(sid, "Pulando etapas 1-4 (arquivo já presente). Indo direto para aplicação...", "cmd")
                            arquivo_existe = True
                        else:
                            log(sid, f"⚠ Arquivo encontrado mas tamanho insuficiente ({tamanho_str}). Prosseguindo com download...", "warn")
                    except:
                        pass
            
            ssh_app_check.close()
            
            # Se arquivo existe e é válido, pula para ETAPA 5
            if arquivo_existe and nome_jar_existente:
                ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
                
                # Ir direto para ETAPA 5
                senha_bd = bd_s['senha'] if bd_s['senha'] else ""
                jdbc = f"jdbc:postgresql://{bd_s['host']}:5432/{bd_s['db_name']}"
                cmd_java = (
                    f"cd {app_s['esus_dir']} && "
                    f"/usr/bin/java -jar \"{nome_jar_existente}\" -console -backup "
                    f"-url=\"{jdbc}\" "
                    f"-username=\"{bd_s['usuario']}\" "
                    f"-password=\"{senha_bd}\""
                )
                
                etapa(5, "Aplicar atualização do e-SUS PEC")
                log(sid, "  Comando que será executado:")
                log(sid, f"  {cmd_java}", "cmd")
                log(sid, "  O serviço será reiniciado automaticamente ao final.", "warn")
                if not aguardar(sid):
                    ssh_app.close()
                    cancela()
                    return
                
                log(sid, "Executando instalador (pode levar vários minutos)...")
                _, err, code = ssh_exec(sid, ssh_app, cmd_java, timeout=3600)
                if code != 0:
                    log(sid, "Instalador encerrou com erro. Veja o log acima.", "warn")
                    log(sid, "Dica: duplique o banco novamente, use o desinstalador e repita.", "warn")
                    falha("Atualização encerrou com erro.")
                    ssh_app.close()
                    return
                
                ok("Atualização concluída! e-SUS PEC reiniciado automaticamente.")
                ssh_app.close()
                
                log(sid, "━" * 50)
                log(sid, f"Ambiente '{amb['nome']}' atualizado para versão {versao}!", "ok")
                log(sid, "━" * 50)
                log(sid, "__FIM_OK__", "controle")
                s["finalizado"] = True
                return
            else:
                log(sid, "Arquivo não encontrado. Prosseguindo com download...", "cmd")
                
        except Exception as e:
            log(sid, f"Aviso ao verificar arquivo: {e}", "warn")
        
        # ── ETAPA 1        # ── ETAPA 1 ───────────────────────────────────────────────────────────
        etapa(1, f"Verificar versão {versao} no site do e-SUS")
        log(sid, "  Site    : https://sisaps.saude.gov.br/esus/")
        log(sid, f"  Arquivo : eSUS-AB-PEC-{versao}-Linux64.jar")
        if url_manual:
            log(sid, f"  URL     : {url_manual} (informada manualmente)")
        if not aguardar(sid):
            cancela()
            return

        url_jar = buscar_url_versao(versao, url_manual, sid=sid)
        if not url_jar:
            falha(
                f"URL de download não encontrada automaticamente para versão {versao}. "
                f"Acesse https://sisaps.saude.gov.br/sistemas/esusaps/blog/versao-{versao.replace('.', '-')} , "
                f"copie o link do botão 'Download para Linux' e informe no campo URL manual."
            )
            return
        nome_jar = url_jar.split("/")[-1]
        ok(f"Arquivo: {nome_jar}")
        log(sid, f"  URL: {url_jar}", "cmd")

        # ── ETAPA 2 ───────────────────────────────────────────────────────────
        etapa(2, f"Parar serviço no servidor de aplicação ({app_s['host']})")
        log(sid, f"  Host    : {app_s['host']}:{app_s['porta']}")
        log(sid, f"  Usuário : {app_s['usuario']}")
        log(sid, f"  Serviço : {app_s['service']}")
        log(sid, "  ⚠ O sistema ficará indisponível durante a atualização.", "warn")
        if not aguardar(sid):
            cancela()
            return

        log(sid, f"Conectando em {app_s['host']}...")
        try:
            ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
        except Exception as e:
            falha(f"Falha ao conectar no servidor de aplicação: {e}")
            return
        ok("Conectado.")
        log(sid, f"Parando serviço {app_s['service']}...")
        _, _, c1 = ssh_exec(sid, ssh_app, f"systemctl stop {app_s['service']}", timeout=15)
        if c1 != 0:
            _, _, c2 = ssh_exec(sid, ssh_app, f"service {app_s['service']} stop", timeout=15)
            if c2 != 0:
                ssh_exec(sid, ssh_app, "pkill -9 -f 'java' || true", timeout=10)
                time.sleep(3)
        ok("Serviço parado.")

        # ── ETAPA 3 ───────────────────────────────────────────────────────────
        data_bkp  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_dump = f"esusbkp{data_bkp}.dump"

        # Pasta de backup: fora do container, mapeada via volume
        # Usa /app/data/backup_dbesus — mapeado no volume esus-data
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backup_dbesus")
        dump_path  = os.path.join(backup_dir, nome_dump)

        etapa(3, f"Backup do banco '{bd_s['db_name']}' (docker exec local)")
        log(sid, f"  Container : {bd_s['container']}")
        log(sid, f"  Banco     : {bd_s['db_name']}")
        log(sid, f"  Destino   : {dump_path}")
        if not aguardar(sid):
            ssh_app.close()
            cancela()
            return

        # Cria a pasta se não existir
        os.makedirs(backup_dir, exist_ok=True)

        # Remove backups anteriores do mesmo banco antes de gerar o novo
        prefixo = f"esusbkp"
        arquivos_antigos = [
            f for f in os.listdir(backup_dir)
            if f.startswith(prefixo) and f.endswith(".dump") and f != nome_dump
        ]
        for arq in arquivos_antigos:
            try:
                os.remove(os.path.join(backup_dir, arq))
                log(sid, f"  Backup anterior removido: {arq}", "cmd")
            except Exception as e:
                log(sid, f"  Aviso: não foi possível remover {arq}: {e}", "warn")

        log(sid, "Executando pg_dump via docker exec...")

        import subprocess
        cmd_dump = [
            "docker", "exec",
            "-e", f"PGPASSWORD={bd_s['senha']}",
            bd_s["container"],
            "pg_dump", "-U", "postgres", "-Fc", bd_s["db_name"]
        ]
        try:
            with open(dump_path, "wb") as f:
                result = subprocess.run(cmd_dump, stdout=f, stderr=subprocess.PIPE, timeout=300)
            if result.returncode != 0:
                err_msg = result.stderr.decode().strip()
                log(sid, f"pg_dump falhou: {err_msg}", "warn")
                log(sid, "Tentando CREATE DATABASE como fallback via SSH no app...", "warn")
                nome_copy = f"bkp_{bd_s['db_name']}_{data_bkp}"
                _, err2, code2 = ssh_exec(sid, ssh_app,
                    f"docker exec {bd_s['container']} psql -U postgres -c "
                    f"\"CREATE DATABASE {nome_copy} WITH TEMPLATE {bd_s['db_name']} OWNER postgres;\""
                )
                if code2 != 0:
                    falha(f"Backup falhou: {err2}")
                    ssh_app.close()
                    return
                ok(f"Backup realizado como banco '{nome_copy}'.")
            else:
                size = os.path.getsize(dump_path)
                ok(f"Backup salvo em {dump_path} ({size//1024} KB)")
        except subprocess.TimeoutExpired:
            falha("Timeout no backup — pg_dump demorou mais de 5 minutos.")
            ssh_app.close()
            return
        except Exception as e:
            falha(f"Erro no backup: {e}")
            ssh_app.close()
            return

        # ── ETAPA 4: Download com verificação de existência ──────────────────────────
        etapa(4, f"Download do instalador ({app_s['host']})")
        log(sid, f"  Diretório : {app_s['esus_dir']}")
        log(sid, f"  Arquivo   : {nome_jar}")
        
        if not aguardar(sid):
            ssh_app.close()
            cancela()
            return

        # Reconecta SSH antes de operações demoradas ou após o backup
        try:
            transport = ssh_app.get_transport()
            if transport is None or not transport.is_active():
                raise Exception("SSH inativo")
            # Testa a sessão
            chan = transport.open_session()
            chan.close()
        except Exception:
            log(sid, "Conexão SSH inativa. Reconectando no servidor de aplicação...", "warn")
            try:
                ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
                ok("Reconectado com sucesso.")
            except Exception as e:
                falha(f"Falha ao reconectar no servidor de aplicação: {e}")
                return

        ok(f"Download concluído: {nome_jar}")

        # ── ETAPA 5 ───────────────────────────────────────────────────────────
        senha_bd = bd_s['senha'] if bd_s['senha'] else ""
        jdbc = f"jdbc:postgresql://{bd_s['host']}:5432/{bd_s['db_name']}"
        cmd_java = (
            f"cd {app_s['esus_dir']} && "
            f"/usr/bin/java -jar \"{nome_jar}\" -console -backup "
            f"-url=\"{jdbc}\" "
            f"-username=\"{bd_s['usuario']}\" "
            f"-password=\"{senha_bd}\""
        )
        etapa(5, "Aplicar atualização do e-SUS PEC")
        log(sid, "  Comando que será executado:")
        log(sid, f"  {cmd_java}", "cmd")
        log(sid, "  O serviço será reiniciado automaticamente ao final.", "warn")
        if not aguardar(sid):
            ssh_app.close()
            cancela()
            return

        # Instala o comando 'file' que o instalador precisa
        log(sid, "Preparando ambiente (instalando dependências)...")
        ssh_exec(sid, ssh_app, "apt-get update -qq && apt-get install -y file -qq 2>/dev/null || true", timeout=60)
        
        log(sid, "Executando instalador (pode levar vários minutos)...")
        # Pipe 'S' para responder automaticamente às perguntas do instalador
        # IMPORTANTE: cd deve estar ANTES do pipe para funcionar corretamente
        cmd_with_input = f"cd {app_s['esus_dir']} && echo 'S' | /usr/bin/java -jar \"{nome_jar}\" -console -backup -url=\"{jdbc}\" -username=\"{bd_s['usuario']}\" -password=\"{senha_bd}\""
        _, err, code = ssh_exec(sid, ssh_app, cmd_with_input, timeout=3600)
        if code != 0:
            log(sid, "Instalador encerrou com erro. Veja o log acima.", "warn")
            log(sid, "Dica: duplique o banco novamente, use o desinstalador e repita.", "warn")
            falha("Atualização encerrou com erro.")
            ssh_app.close()
            return

        # Aguarda um pouco para o instalador finalizar
        import time
        time.sleep(5)
        
        # Força restart do container Docker para garantir que a atualização seja aplicada
        log(sid, "Reiniciando container Docker...")
        if app_s.get('usa_docker'):
            container_name = app_s.get('container', 'e-SUS-PEC')
            _, _, restart_code = ssh_exec(sid, ssh_app, f"docker restart {container_name}", timeout=60)
            if restart_code == 0:
                log(sid, f"Container {container_name} reiniciado com sucesso.", "ok")
                time.sleep(10)  # Aguarda container ficar pronto
            else:
                log(sid, f"Aviso: Não foi possível reiniciar o container. Reinicie manualmente.", "warn")
        
        ok("Atualização concluída! e-SUS PEC reiniciado automaticamente.")
        ssh_app.close()

        log(sid, "━" * 50)
        log(sid, f"Ambiente '{amb['nome']}' atualizado para versão {versao}!", "ok")
        log(sid, f"Backup em: {dump_path}", "ok")
        log(sid, "━" * 50)
        log(sid, "__FIM_OK__", "controle")
        s["finalizado"] = True

    except Exception as e:
        falha(f"Erro inesperado: {e}")
        import traceback
        log(sid, traceback.format_exc(), "erro")


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
            "INSERT INTO srv_app (ambiente_id, host, porta, usuario, senha, esus_dir, service, usa_docker) VALUES (?,?,?,?,?,?,?,?)",
            (aid, d.get("app_host", "localhost"), d.get("app_porta", 22), d.get("app_usuario", "root"), 
             d.get("app_senha", ""), d.get("app_dir", "/opt/e-SUS"), d.get("app_service", "e-SUS-PEC"), 1 if d.get("app_docker") else 0)
        )
        db.execute(
            "INSERT INTO srv_bd (ambiente_id, host, porta, usuario, senha, db_name, container) VALUES (?,?,?,?,?,?,?)",
            (aid, d.get("bd_host", "localhost"), d.get("bd_porta", 22), d.get("bd_usuario", "root"),
             d.get("bd_senha", ""), d.get("bd_nome", "novosgar"), d.get("bd_container", "postgresql-db-1"))
        )
    return jsonify({"id": aid}), 201

@app.route("/api/ambientes/<int:aid>", methods=["PUT"])
@login_requerido
def api_editar_ambiente(aid):
    d = request.get_json(silent=True) or {}
    with get_db() as db:
        db.execute("UPDATE ambientes SET nome=?, tipo=? WHERE id=?", (d.get("nome"), d.get("tipo", "homologacao"), aid))
        db.execute("""
            UPDATE srv_app SET host=?, porta=?, usuario=?, senha=?, esus_dir=?, service=?, usa_docker=?
            WHERE ambiente_id=?
        """, (d.get("app_host", "localhost"), d.get("app_porta", 22), d.get("app_usuario", "root"), 
              d.get("app_senha", ""), d.get("app_dir", "/opt/e-SUS"), d.get("app_service", "e-SUS-PEC"), 1 if d.get("app_docker") else 0, aid))
        db.execute("""
            UPDATE srv_bd SET host=?, porta=?, usuario=?, senha=?, db_name=?, container=?
            WHERE ambiente_id=?
        """, (d.get("bd_host", "localhost"), d.get("bd_porta", 22), d.get("bd_usuario", "root"),
              d.get("bd_senha", ""), d.get("bd_nome", "novosgar"), d.get("bd_container", "postgresql-db-1"), aid))
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)