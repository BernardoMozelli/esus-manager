# e-SUS PEC — Gerenciador de Atualizações
**Prefeitura Municipal de Santa Luzia / MG**

---

## Requisitos
- Docker
- Docker Compose

---

## Iniciar

```bash
docker compose up -d
```

Acesse: **http://localhost:5000**

---

## Parar

```bash
docker compose down
```

---

## Ver logs da aplicação

```bash
docker compose logs -f
```

---

## Atualizar após mudanças no código

```bash
docker compose up -d --build
```

---

## Dados persistidos

O banco SQLite com os ambientes cadastrados fica no volume Docker `esus-data`.
Para fazer backup manual:

```bash
docker cp esus-manager:/app/data/config.db ./config_backup.db
```

---

## Portas

| Serviço        | Porta |
|----------------|-------|
| Aplicação web  | 5000  |

Para mudar a porta, edite o `docker-compose.yml`:
```yaml
ports:
  - "8080:5000"  # acesso em :8080
```
