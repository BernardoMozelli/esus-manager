import subprocess, sys, json, re, time, tempfile, os

debug = []

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr

# Tenta vários métodos para instalar playwright
def instalar_playwright():
    # Método 1: pip3 install direto
    c, o = run(["pip3", "install", "playwright", "-q"])
    debug.append(f"pip3: rc={c}")
    if c == 0:
        return True

    # Método 2: pip3 com --break-system-packages
    c, o = run(["pip3", "install", "playwright", "--break-system-packages", "-q"])
    debug.append(f"pip3 --break: rc={c}")
    if c == 0:
        return True

    # Método 3: apt-get install python3-pip primeiro
    run(["apt-get", "update", "-qq"])
    run(["apt-get", "install", "-y", "-q", "python3-pip"])
    c, o = run(["pip3", "install", "playwright", "-q"])
    debug.append(f"pip3 after apt: rc={c}")
    if c == 0:
        return True

    # Método 4: curl get-pip.py
    c, o = run(["curl", "-s", "https://bootstrap.pypa.io/get-pip.py", "-o", "/tmp/get-pip.py"])
    if c == 0:
        run([sys.executable, "/tmp/get-pip.py", "-q"])
        c, o = run([sys.executable, "-m", "pip", "install", "playwright", "-q"])
        debug.append(f"get-pip: rc={c}")
        if c == 0:
            return True

    return False

# Verifica se playwright já está disponível
c, _ = run([sys.executable, "-c", "import playwright"])
if c == 0:
    debug.append("playwright: ok")
else:
    debug.append("instalando playwright...")
    if not instalar_playwright():
        print(json.dumps({"erro_geral": "Nao foi possivel instalar playwright", "debug": debug}))
        sys.exit(1)

    # Instala dependências do sistema para Chromium
    run(["apt-get", "update", "-qq"])
    run(["apt-get", "install", "-y", "-q",
         "libnss3", "libatk1.0-0", "libatk-bridge2.0-0", "libcups2",
         "libdrm2", "libxkbcommon0", "libxcomposite1", "libxdamage1",
         "libxfixes3", "libxrandr2", "libgbm1", "libasound2",
         "libpango-1.0-0", "libpangocairo-1.0-0", "libx11-6", "libxext6"])

    # Instala Chromium via playwright
    c, o = run(["python3", "-m", "playwright", "install", "chromium"])
    debug.append(f"chromium: rc={c} {o[:100]}")

# Script Playwright em arquivo temporário
inner_lines = [
    "import json, re, time, sys",
    "from playwright.sync_api import sync_playwright",
    "BLOG='https://sisaps.saude.gov.br/sistemas/esusaps/blog/'",
    "BASE='https://sisaps.saude.gov.br'",
    "debug=[]",
    "def ok(v):",
    "    try: return [int(x) for x in v.split('.')] >= [5,4,30]",
    "    except: return False",
    "res=[]",
    "try:",
    "    with sync_playwright() as pw:",
    "        br=pw.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])",
    "        ctx=br.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',locale='pt-BR')",
    "        pg=ctx.new_page()",
    "        pg.goto(BLOG,wait_until='domcontentloaded',timeout=30000)",
    "        html=pg.content()",
    "        slugs=list(dict.fromkeys(re.findall(r'/blog/(versao-[0-9]+-[0-9]+-[0-9]+)',html)))",
    "        slugs=[s for s in slugs if ok(s.replace('versao-','').replace('-','.'))]",
    "        debug.append('SLUGS:'+str(slugs))",
    "        pg.close()",
    "        for slug in slugs:",
    "            v=slug.replace('versao-','').replace('-','.')",
    "            ul=uw=None",
    "            try:",
    "                p2=ctx.new_page()",
    "                caps=[]",
    "                p2.on('request',lambda r,c=caps:c.append(r.url) if '.jar' in r.url else None)",
    "                p2.goto(BASE+'/sistemas/esusaps/blog/'+slug+'/',wait_until='domcontentloaded',timeout=20000)",
    "                try:",
    "                    btn=p2.locator(\"button:has-text('Download para Linux')\").first",
    "                    btn.wait_for(timeout=8000)",
    "                    btn.click()",
    "                    p2.wait_for_timeout(3000)",
    "                except Exception as e: debug.append('CLICK '+v+':'+str(e)[:80])",
    "                p2.close()",
    "                for u in caps:",
    "                    if 'Linux64' in u: ul=u",
    "                    elif 'Win64' in u: uw=u",
    "                debug.append(('URL ' if ul else 'NOURL ')+v+((':'+ul[:70]) if ul else (' caps='+str(caps))))",
    "            except Exception as e: debug.append('ERR '+v+':'+str(e)[:80])",
    "            res.append({'versao':v,'url_linux':ul,'url_windows':uw})",
    "            time.sleep(0.5)",
    "        br.close()",
    "except Exception as e:",
    "    print(json.dumps({'erro_geral':str(e),'debug':debug}))",
    "    sys.exit(1)",
    "print(json.dumps({'resultado':res,'debug':debug}))",
]

with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
    f.write('\n'.join(inner_lines) + '\n')
    tmp = f.name

r = subprocess.run(["python3", tmp], capture_output=True, text=True)
os.unlink(tmp)
out = r.stdout + r.stderr
debug.append(f"inner exit={r.returncode}")

lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
json_line = next((l for l in reversed(lines) if l.startswith('[') or l.startswith('{')), None)
if json_line:
    try:
        inner = json.loads(json_line)
        if isinstance(inner, dict) and 'resultado' in inner:
            inner['debug'] = debug + inner.get('debug', [])
            print(json.dumps(inner))
        else:
            print(json.dumps({"erro_geral": str(inner), "debug": debug + lines[-10:]}))
    except Exception as e:
        print(json.dumps({"erro_geral": f"parse: {e}", "debug": debug + lines[-10:]}))
else:
    print(json.dumps({"erro_geral": "sem JSON", "debug": debug + lines[-15:]}))