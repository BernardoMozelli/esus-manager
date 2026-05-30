<div align="center">
  <h1>e-SUS PEC — Gerenciador de Atualizações</h1>
  <p><strong>Prefeitura Municipal de Santa Luzia · MG</strong></p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.11-blue?style=flat-square&logo=python" />
    <img src="https://img.shields.io/badge/Flask-2.3+-green?style=flat-square&logo=flask" />
    <img src="https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker" />
    <img src="https://img.shields.io/badge/PostgreSQL-via%20SSH-336791?style=flat-square&logo=postgresql" />
  </p>
</div>

---

## Sobre

Sistema web para gerenciar atualizações, downgrades e backups do **e-SUS PEC** em servidores remotos via SSH, com interface unificada para ambientes de homologação e produção.

Desenvolvido pela **Secretaria Municipal de Ciência, Planejamento, Tecnologia e Inovação** da Prefeitura Municipal de Santa Luzia — MG.

**Desenvolvedor:** Bernardo Mozelli

---

## Funcionalidades

| Módulo | Descrição |
|---|---|
| **Atualizar Sistema** | Atualiza o e-SUS PEC para uma versão mais recente com 5 etapas: verificar versão, parar serviço, backup, download e instalação |
| **Downgrade** | Reverte para uma versão anterior com desinstalador nativo, backup automático e reinstalação |
| **Backup** | Gera backup manual do banco PostgreSQL via `pg_dump` com log em tempo real e histórico |
| **Versões** | Descobre automaticamente as URLs de download no site do Ministério da Saúde |
| **Ambientes** | Cadastra servidores (SSH app + SSH banco) para homologação e produção |
| **Usuários** | Gerencia quem tem acesso ao sistema |

### Características técnicas

- Execução via **SSH** nos servidores remotos — sem agente instalado
- **Streaming de logs** em tempo real via SSE (Server-Sent Events)
- Confirmação manual por etapa ou **modo automático** (Modo Barion)
- **Backup automático** antes de qualquer atualização ou downgrade
- Banco SQLite local para configurações e histórico
- Hot-reload de código sem rebuild da imagem Docker

---

## Requisitos

- Docker
- Docker Compose
- Acesso SSH aos servidores de aplicação e banco do e-SUS PEC
- Rede Docker externa `esus_teste_default` (ver abaixo)

---

## Instalação

### 1. Criar a rede Docker (se não existir)

```bash
docker network create esus_teste_default
```

### 2. Clonar ou extrair o projeto

```bash
unzip esus-manager.zip
cd esus-manager
```

### 3. Subir a aplicação

```bash
docker compose up -d --build
```

Acesse: **http://localhost:5000**

---

## Login padrão

| Campo | Valor |
|---|---|
| Usuário | `administrador` |
| Senha | `6p4d0b@2026` |

> Troque a senha após o primeiro acesso em **Menu do usuário → Gerenciar Usuários**.

---

## Estrutura do projeto

```
esus-manager/
├── backend/
│   ├── app.py              # API Flask + lógica de atualização/downgrade/backup
│   ├── sync_script.py      # Script de descoberta de versões (Playwright)
│   ├── requirements.txt
│   ├── Dockerfile
│   └── entrypoint.sh       # Hot-reload via inotifywait
├── frontend/
│   ├── templates/
│   │   ├── index.html      # Interface principal
│   │   └── login.html      # Tela de login
│   └── static/
│       ├── css/style.css
│       ├── js/
│       │   ├── main.js     # Atualização de sistema
│       │   ├── backup.js   # Módulo de backup
│       │   ├── downgrade.js# Módulo de downgrade
│       │   └── users.js    # Gerenciamento de usuários
│       └── img/
│           ├── logo.png
│           └── favicon.ico
├── docker-compose.yml
└── README.md
```

---

## Operação

### Ver logs da aplicação

```bash
docker compose logs -f
```

### Parar

```bash
docker compose down
```

### Atualizar código (sem rebuild)

Os arquivos `backend/app.py` e `frontend/` são montados como volumes — edite no host e o Flask reinicia automaticamente via `inotifywait`.

### Rebuild completo (mudança de dependências)

```bash
docker compose up -d --build
```

---

## Dados persistidos

| Dado | Local |
|---|---|
| Banco SQLite (ambientes, versões, usuários, histórico de backups) | Volume Docker `esus-data` → `/app/data/config.db` |
| Arquivos de backup `.dump` | `./backup_dbesus/` (bind mount no host) |

### Backup manual do SQLite

```bash
docker cp esus-manager:/app/data/config.db ./config_backup.db
```

---

## Portas

| Serviço | Porta |
|---|---|
| Aplicação web | `5000` |

Para alterar, edite o `docker-compose.yml`:

```yaml
ports:
  - "8080:5000"   # acesso em :8080
```

---

## Fluxo de Atualização

```
1. Verificar versão no site do e-SUS
2. Parar serviço no servidor de aplicação
3. Backup do banco de dados (pg_dump local)
4. Download do instalador no servidor remoto
5. Aplicar atualização (java -jar ... -console)
```

## Fluxo de Downgrade

```
1. Parar serviço no servidor
2. Backup de segurança do banco
3. Executar desinstalador nativo (limpa travas de versão)
4. Download do instalador da versão anterior
5. Instalar versão de destino
```

---

## Segurança

- Autenticação por sessão Flask
- Senhas armazenadas com hash SHA-256
- Todas as rotas da API exigem login (`@login_requerido`)
- Acesso ao painel de usuários restrito ao perfil **Administrador**
- Credenciais SSH armazenadas localmente no SQLite

---

<div align="center">
  <sub>Secretaria Municipal de Ciência, Planejamento, Tecnologia e Inovação · Santa Luzia · MG</sub>
</div>
