# Instalação nativa em Linux

Para desenvolvimento. Em uso normal, prefira [Docker](docker.md).

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
docker compose up -d chromadb searxng   # serviços auxiliares
./venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7000
```

**Cuidado se a stack Docker já estiver rodando.** O `.env` costuma apontar `DATABASE_URL` para `data/app.db`, o mesmo arquivo do container. Para testar sem tocar nos dados reais:

```bash
ODYSSEUS_DATA_DIR=/tmp/ody DATABASE_URL=sqlite:////tmp/ody/app.db \
ODYSSEUS_INPROCESS_TASKS=0 ODYSSEUS_INPROCESS_POLLERS=0 \
./venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7001
```

Testes: `./venv/bin/python -m pytest tests/jarvis`.
