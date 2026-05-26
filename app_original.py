from flask import Flask, render_template, request, jsonify, Response
import paramiko, requests, re, threading, queue, time, datetime, json, uuid, urllib3, sqlite3, os

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
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
        """)

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
        """, (aid, "localhost", 22, "root", "rootpassword", "esus", "esus_dblocal"))

init_db()
seed_docker()

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
    chan = client.get_transport().open_session()
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

# ─── e-SUS: descoberta de URL de download ─────────────────────────────────────

JAR_BASE = "https://arquivos.esusab.ufsc.br/PEC"

_cache_hashes = {}

# Hashes conhecidos para versões testadas
HASHES_CONHECIDOS = {
    "5.3.20": "3262e8f6b7bf8d66",
    "5.3.21": "1af9b7ee9c3886bd",
    "5.3.22": "3262e8f6b7bf8d66",
    "5.3.26": "3262e8f6b7bf8d66",
    "5.3.28": "3262e8f6b7bf8d66",
    "5.2.28": "mtRazOmMxfBpkEMK",
    "5.4.15": "iETBUgrZGSiVAGXT",
    "5.4.28": "Gu7DmBMVQCLxaEkw",
    "5.4.30": "Gu7DmBMVQCLxaEkw",
    "5.4.31": "Gu7DmBMVQCLxaEkw",
    "5.4.33": "Gu7DmBMVQCLxaEkw",
    "5.4.36": "Gu7DmBMVQCLxaEkw",
    "5.4.37": "Gu7DmBMVQCLxaEkw",
}

def _tentar_url(url, timeout=10):
    """Retorna True se a URL responde com 200 ou 206 (arquivo existe)."""
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True, verify=False)
        return r.status_code in (200, 206)
    except Exception:
        return False

def descobrir_url_por_scraping(versao, sid=None):
    """
    Tenta descobrir a URL do JAR fazendo scraping HTTP simples da página do blog,
    sem depender de Playwright (que muitas vezes não está disponível).
    """
    def _log(msg):
        if sid and sid in sessoes:
            log(sid, f"  [scraping] {msg}", "cmd")

    slug = versao.replace(".", "-")
    urls_tentadas = [
        f"https://sisaps.saude.gov.br/sistemas/esusaps/blog/versao-{slug}",
        f"http://sisaps.saude.gov.br/sistemas/esusaps/blog/versao-{slug}",
    ]

    for url_blog in urls_tentadas:
        try:
            _log(f"Buscando página: {url_blog}")
            r = requests.get(url_blog, timeout=20, verify=False,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                _log(f"HTTP {r.status_code}, tentando próxima URL...")
                continue

            # Busca links para .jar no conteúdo da página
            padrao = r'https?://[^\s"\'<>]+Linux64\.jar'
            matches = re.findall(padrao, r.text)
            if matches:
                url_jar = matches[0]
                _log(f"URL encontrada na página: {url_jar}")
                # Extrai hash do caminho para cache
                m = re.search(r'/PEC/([^/]+)/', url_jar)
                if m:
                    _cache_hashes[versao] = m.group(1)
                return url_jar
            else:
                _log("Nenhum link .jar encontrado na página.")
        except Exception as e:
            _log(f"Erro ao acessar {url_blog}: {e}")

    return None

def descobrir_url_por_probe(versao, sid=None):
    """
    Estratégia de último recurso: testa URLs conhecidas para a versão,
    variando padrões de hash frequentes.
    """
    def _log(msg):
        if sid and sid in sessoes:
            log(sid, f"  [probe] {msg}", "cmd")

    nome_jar = f"eSUS-AB-PEC-{versao}-Linux64.jar"

    # Testa se algum hash do cache funciona para esta versão
    hashes_para_testar = list(set(list(_cache_hashes.values()) + list(HASHES_CONHECIDOS.values())))

    _log(f"Testando {len(hashes_para_testar)} hashes conhecidos para versão {versao}...")
    for h in hashes_para_testar:
        url = f"{JAR_BASE}/{h}/{versao}/{nome_jar}"
        if _tentar_url(url):
            _log(f"Hash encontrado: {h}")
            _cache_hashes[versao] = h
            return url

    return None

def buscar_url_versao(versao, url_manual=None, sid=None):
    """
    Resolve a URL de download do JAR para a versão indicada.
    Ordem de tentativas:
      1. URL informada manualmente
      2. Hash em cache
      3. Hash na lista estática HASHES_CONHECIDOS
      4. Scraping HTTP da página do blog
      5. Probe por hashes conhecidos
    """
    if url_manual:
        return url_manual

    # Cache em memória
    if versao in _cache_hashes:
        h = _cache_hashes[versao]
        return f"{JAR_BASE}/{h}/{versao}/eSUS-AB-PEC-{versao}-Linux64.jar"

    # Lista estática
    if versao in HASHES_CONHECIDOS:
        h = HASHES_CONHECIDOS[versao]
        _cache_hashes[versao] = h
        return f"{JAR_BASE}/{h}/{versao}/eSUS-AB-PEC-{versao}-Linux64.jar"

    if sid and sid in sessoes:
        log(sid, f"  Hash desconhecido para versão {versao}. Buscando automaticamente...", "warn")

    # Scraping HTTP (sem Playwright)
    url = descobrir_url_por_scraping(versao, sid=sid)
    if url:
        return url

    # Probe por hashes
    url = descobrir_url_por_probe(versao, sid=sid)
    if url:
        return url

    return None

def buscar_versoes_disponiveis():
    versoes = [
        "5.4.37", "5.4.36", "5.4.33", "5.4.31", "5.4.30",
        "5.4.28", "5.4.26", "5.4.22", "5.4.21", "5.4.14",
        "5.4.13", "5.4.11", "5.4.10", "5.4.9",  "5.4.8",
        "5.4.4",  "5.3.28", "5.3.26", "5.3.22", "5.3.20",
    ]
    return [{"versao": v, "arquivo": f"eSUS-AB-PEC-{v}-Linux64.jar"} for v in versoes]


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

    def ok(msg):    log(sid, f"✅ {msg}", "ok")
    def etapa(n,t): log(sid, f"__ETAPA__{n}__{t}", "etapa")
    def cancela():
        log(sid, "Operação cancelada pelo usuário.", "warn")
        s["finalizado"] = True

    try:
        # ── ETAPA 1 ───────────────────────────────────────────────────────────
        etapa(1, f"Verificar versão {versao} no site do e-SUS")
        log(sid, "  Site    : https://sisaps.saude.gov.br/esus/")
        log(sid, f"  Arquivo : eSUS-AB-PEC-{versao}-Linux64.jar")
        if url_manual:
            log(sid, f"  URL     : {url_manual} (informada manualmente)")
        if not aguardar(sid): cancela(); return

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
        if not aguardar(sid): cancela(); return

        log(sid, f"Conectando em {app_s['host']}...")
        try:
            ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
        except Exception as e:
            falha(f"Falha ao conectar no servidor de aplicação: {e}"); return
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
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
        dump_path  = os.path.join(backup_dir, nome_dump)

        etapa(3, f"Backup do banco '{bd_s['db_name']}' (docker exec local)")
        log(sid, f"  Container : {bd_s['container']}")
        log(sid, f"  Banco     : {bd_s['db_name']}")
        log(sid, f"  Destino   : {dump_path}")
        if not aguardar(sid): ssh_app.close(); cancela(); return

        os.makedirs(backup_dir, exist_ok=True)
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
                    falha(f"Backup falhou: {err2}"); ssh_app.close(); return
                ok(f"Backup realizado como banco '{nome_copy}'.")
            else:
                size = os.path.getsize(dump_path)
                ok(f"Backup salvo em {dump_path} ({size//1024} KB)")
        except subprocess.TimeoutExpired:
            falha("Timeout no backup — pg_dump demorou mais de 5 minutos."); ssh_app.close(); return
        except Exception as e:
            falha(f"Erro no backup: {e}"); ssh_app.close(); return

        # ── ETAPA 4 ───────────────────────────────────────────────────────────
        etapa(4, f"Download do instalador ({app_s['host']})")
        log(sid, f"  Diretório : {app_s['esus_dir']}")
        log(sid, f"  Arquivo   : {nome_jar}")
        log(sid, f"  URL       : {url_jar}")
        if not aguardar(sid): ssh_app.close(); cancela(); return

        # Reconecta SSH — pode ter expirado durante o backup
        try:
            ssh_app.close()
        except Exception:
            pass
        log(sid, "Reconectando no servidor de aplicação...")
        try:
            ssh_app = ssh_connect(app_s["host"], app_s["porta"], app_s["usuario"], app_s["senha"])
        except Exception as e:
            falha(f"Falha ao reconectar no servidor de aplicação: {e}"); return
        ok("Reconectado.")

        ssh_exec(sid, ssh_app, f"mkdir -p {app_s['esus_dir']}")

        # Verifica se o arquivo já foi baixado anteriormente (evita re-download)
        _, out_check, _ = ssh_exec(sid, ssh_app,
            f"test -f {app_s['esus_dir']}/{nome_jar} && du -sh {app_s['esus_dir']}/{nome_jar} || echo '__NAO_EXISTE__'",
            timeout=10
        )
        if "__NAO_EXISTE__" not in out_check and out_check.strip():
            log(sid, f"  Arquivo já existe no servidor: {out_check.strip()}", "ok")
            log(sid, "  Pulando download (arquivo já presente).", "cmd")
        else:
            log(sid, "Verificando ferramentas de download...")
            _, _, code_wget = ssh_exec(sid, ssh_app, "which wget")
            _, _, code_curl = ssh_exec(sid, ssh_app, "which curl")

            if code_wget != 0 and code_curl != 0:
                log(sid, "Instalando wget...")
                ssh_exec(sid, ssh_app, "apt-get update -qq && apt-get install -y wget -qq", timeout=120)
                _, _, code_wget = ssh_exec(sid, ssh_app, "which wget")

            log(sid, "Baixando arquivo (pode levar vários minutos para ~1GB)...")
            # wget com --progress=dot:giga: 1 linha a cada 100MB — bem menos ruído
            # is_download=True filtra as linhas de progresso e mostra apenas marcos de 10%
            if code_wget == 0:
                _, err, code = ssh_exec(sid, ssh_app,
                    f"cd {app_s['esus_dir']} && wget --no-check-certificate --progress=dot:giga -O '{nome_jar}' '{url_jar}' 2>&1",
                    timeout=1800, is_download=True
                )
            else:
                _, err, code = ssh_exec(sid, ssh_app,
                    f"cd {app_s['esus_dir']} && curl -k -L -# -o '{nome_jar}' '{url_jar}' 2>&1",
                    timeout=1800, is_download=True
                )

            if code != 0:
                # Remove arquivo parcial antes de tentar novamente
                ssh_exec(sid, ssh_app, f"rm -f {app_s['esus_dir']}/{nome_jar}", timeout=10)
                # Tenta redescobrir a URL e repetir o download uma vez
                log(sid, "Download falhou. Tentando redescobrir URL...", "warn")
                if versao in _cache_hashes:
                    del _cache_hashes[versao]  # Invalida cache para forçar nova busca
                url_jar2 = buscar_url_versao(versao, None, sid=sid)
                if url_jar2 and url_jar2 != url_jar:
                    log(sid, f"  Nova URL: {url_jar2}", "cmd")
                    nome_jar2 = url_jar2.split("/")[-1]
                    if code_wget == 0:
                        _, err, code = ssh_exec(sid, ssh_app,
                            f"cd {app_s['esus_dir']} && wget --no-check-certificate --progress=dot:giga -O '{nome_jar2}' '{url_jar2}' 2>&1",
                            timeout=1800, is_download=True
                        )
                    else:
                        _, err, code = ssh_exec(sid, ssh_app,
                            f"cd {app_s['esus_dir']} && curl -k -L -# -o '{nome_jar2}' '{url_jar2}' 2>&1",
                            timeout=1800, is_download=True
                        )
                    if code == 0:
                        nome_jar = nome_jar2
                        url_jar  = url_jar2
                    else:
                        falha(f"Falha no download mesmo após nova tentativa: {err}"); ssh_app.close(); return
                else:
                    falha(f"Falha no download: {err}"); ssh_app.close(); return

        ok(f"Download concluído: {nome_jar}")

        # ── ETAPA 5 ───────────────────────────────────────────────────────────
        jdbc     = f"jdbc:postgresql://{bd_s['host']}:5432/{bd_s['db_name']}"
        cmd_java = (
            f"cd {app_s['esus_dir']} && "
            f"java -jar \"{nome_jar}\" -console -backup "
            f"-url=\"{jdbc}\" -username=\"postgres\" -password=\"\""
        )
        etapa(5, "Aplicar atualização do e-SUS PEC")
        log(sid, "  Comando que será executado:")
        log(sid, f"  {cmd_java}", "cmd")
        log(sid, "  O serviço será reiniciado automaticamente ao final.", "warn")
        if not aguardar(sid): ssh_app.close(); cancela(); return

        log(sid, "Executando instalador (pode levar vários minutos)...")
        _, err, code = ssh_exec(sid, ssh_app, cmd_java, timeout=3600)
        if code != 0:
            log(sid, "Instalador encerrou com erro. Veja o log acima.", "warn")
            log(sid, "Dica: duplique o banco novamente, use o desinstalador e repita.", "warn")
            falha("Atualização encerrou com erro."); ssh_app.close(); return

        ok("Atualização concluída! e-SUS PEC reiniciado automaticamente.")
        ssh_app.close()

        log(sid, "━" * 50)
        log(sid, f"Ambiente '{amb['nome']}' atualizado para versão {versao}!", "ok")
        log(sid, f"Backup em: {dump_path}", "ok")
        log(sid, "━" * 50)
        log(sid, "__FIM_OK__", "controle")
        s["finalizado"] = True

    except Exception as e:
        import traceback
        falha(f"Erro inesperado: {e}\n{traceback.format_exc()}")

# ─── Rotas: CRUD ambientes ────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/ambientes", methods=["GET"])
def listar_ambientes():
    with get_db() as db:
        rows = db.execute("""
            SELECT a.id, a.nome, a.tipo, a.criado,
                   sa.host AS app_host,
                   sb.host AS bd_host, sb.db_name
            FROM ambientes a
            LEFT JOIN srv_app sa ON sa.ambiente_id = a.id
            LEFT JOIN srv_bd  sb ON sb.ambiente_id  = a.id
            ORDER BY a.id
        """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/ambientes/<int:aid>", methods=["GET"])
def detalhe_ambiente(aid):
    amb = get_ambiente_full(aid)
    if not amb:
        return jsonify({"erro": "Não encontrado"}), 404
    return jsonify(amb)

@app.route("/api/ambientes", methods=["POST"])
def criar_ambiente():
    d = request.json
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO ambientes (nome, tipo) VALUES (?,?)",
            (d["nome"], d.get("tipo", "homologacao"))
        )
        aid = cur.lastrowid
        db.execute("""
            INSERT INTO srv_app (ambiente_id, host, porta, usuario, senha, esus_dir, service, usa_docker)
            VALUES (?,?,?,?,?,?,?,?)
        """, (aid, d["app_host"], d.get("app_porta", 22), d["app_usuario"], d["app_senha"],
              d.get("app_dir", "/opt/e-SUS"), d.get("app_service", "e-SUS-PEC"),
              1 if d.get("app_docker") else 0))
        db.execute("""
            INSERT INTO srv_bd (ambiente_id, host, porta, usuario, senha, db_name, container)
            VALUES (?,?,?,?,?,?,?)
        """, (aid, d["bd_host"], d.get("bd_porta", 22), d["bd_usuario"], d["bd_senha"],
              d["bd_nome"], d.get("bd_container", "postgresql-db-1")))
    return jsonify({"ok": True, "id": aid})

@app.route("/api/ambientes/<int:aid>", methods=["PUT"])
def editar_ambiente(aid):
    d = request.json
    with get_db() as db:
        db.execute("UPDATE ambientes SET nome=?, tipo=? WHERE id=?",
                   (d["nome"], d.get("tipo", "homologacao"), aid))
        db.execute("""
            UPDATE srv_app SET host=?,porta=?,usuario=?,senha=?,esus_dir=?,service=?,usa_docker=?
            WHERE ambiente_id=?
        """, (d["app_host"], d.get("app_porta", 22), d["app_usuario"], d["app_senha"],
              d.get("app_dir", "/opt/e-SUS"), d.get("app_service", "e-SUS-PEC"),
              1 if d.get("app_docker") else 0, aid))
        db.execute("""
            UPDATE srv_bd SET host=?,porta=?,usuario=?,senha=?,db_name=?,container=?
            WHERE ambiente_id=?
        """, (d["bd_host"], d.get("bd_porta", 22), d["bd_usuario"], d["bd_senha"],
              d["bd_nome"], d.get("bd_container", "postgresql-db-1"), aid))
    return jsonify({"ok": True})

@app.route("/api/ambientes/<int:aid>", methods=["DELETE"])
def deletar_ambiente(aid):
    with get_db() as db:
        db.execute("DELETE FROM ambientes WHERE id=?", (aid,))
    return jsonify({"ok": True})

# ─── Rotas: versões e execução ────────────────────────────────────────────────

@app.route("/api/versoes")
def listar_versoes():
    return jsonify(buscar_versoes_disponiveis())

@app.route("/api/resolver-url", methods=["POST"])
def resolver_url():
    """Resolve a URL de download para uma versão sem iniciar a atualização."""
    d = request.json
    versao = d.get("versao", "")
    url_manual = d.get("url_manual")
    if not versao:
        return jsonify({"erro": "versao obrigatória"}), 400

    url = buscar_url_versao(versao, url_manual)
    if url:
        return jsonify({"ok": True, "url": url})
    return jsonify({"ok": False, "url": None,
                    "erro": f"URL não encontrada para versão {versao}. Informe manualmente."})

@app.route("/api/iniciar", methods=["POST"])
def iniciar():
    d   = request.json
    amb = get_ambiente_full(d["ambiente_id"])
    if not amb:
        return jsonify({"erro": "Ambiente não encontrado"}), 404
    modo_auto = bool(d.get("modo_auto", False))
    sid = nova_sessao(modo_auto=modo_auto)
    threading.Thread(target=executar_atualizacao,
                     args=(sid, amb, d["versao"], d.get("url_download")), daemon=True).start()
    return jsonify({"session_id": sid, "modo_auto": modo_auto})

@app.route("/api/confirmar/<sid>", methods=["POST"])
def confirmar(sid):
    if sid not in sessoes:
        return jsonify({"ok": False})
    sessoes[sid]["cancelado"] = False
    sessoes[sid]["aguardando"].set()
    return jsonify({"ok": True})

@app.route("/api/cancelar/<sid>", methods=["POST"])
def cancelar(sid):
    if sid not in sessoes:
        return jsonify({"ok": False})
    sessoes[sid]["cancelado"] = True
    sessoes[sid]["aguardando"].set()
    return jsonify({"ok": True})

@app.route("/api/stream/<sid>")
def stream(sid):
    def generate():
        if sid not in sessoes:
            return
        q = sessoes[sid]["log_queue"]
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {msg}\n\n"
                p = json.loads(msg)
                if p.get("tipo") == "controle" and p["msg"] in ("__FIM_OK__", "__FIM_ERRO__"):
                    break
            except queue.Empty:
                yield 'data: {"ts":"","msg":"__PING__","tipo":"ping"}\n\n'
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
