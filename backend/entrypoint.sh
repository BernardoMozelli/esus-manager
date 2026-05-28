#!/bin/sh
# Entrypoint com hot-reload: reinicia o Flask quando qualquer .py mudar

echo "[entrypoint] Iniciando e-SUS Manager com hot-reload..."

# Função para iniciar o Flask em background
start_flask() {
    python /app/app.py &
    FLASK_PID=$!
    echo "[entrypoint] Flask iniciado (PID $FLASK_PID)"
}

# Função para parar o Flask
stop_flask() {
    if [ -n "$FLASK_PID" ]; then
        kill "$FLASK_PID" 2>/dev/null
        wait "$FLASK_PID" 2>/dev/null
    fi
}

start_flask

# Monitora mudanças em .py usando inotifywait (inotify-tools)
# Monitora backend (app.py, sync_script.py) — frontend é servido diretamente do volume
while inotifywait -e close_write,moved_to,create /app/app.py /app/sync_script.py 2>/dev/null; do
    echo "[entrypoint] Arquivo alterado — reiniciando Flask..."
    stop_flask
    sleep 0.5
    start_flask
done

# Fallback: se inotifywait não estiver disponível, roda o Flask direto com auto-reload do Werkzeug
wait $FLASK_PID
