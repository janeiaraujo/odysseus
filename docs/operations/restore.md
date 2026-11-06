# Restauração e migração para outro computador

**Estado: demonstrado.** Um backup real foi restaurado em diretório limpo, com um ChromaDB novo, e a aplicação subiu servindo sessões, endpoints com chaves legíveis, tarefas e vetores.

Clonar recupera o código; restaurar recupera o estado. São passos separados.

```bash
git clone https://github.com/janeiaraujo/odysseus.git jarvis && cd jarvis
git checkout jarvis/dev
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
docker compose up -d chromadb

./venv/bin/python scripts/jarvis-backup restore --key-file CHAVE ARQUIVO.jrvb --data-dir data --env-target .env
./venv/bin/python scripts/jarvis-backup validate --data-dir data
docker compose up -d --build
python3 scripts/jarvis-doctor
```

**Segurança:** o backup é verificado antes de tocar no destino. Um destino com dados recusa a operação; com `--force`, o conteúdo atual é movido para `data.pre-restore-<data>`, nunca sobrescrito.

Chaves externas (como a do Firebase) voltam para `~/.config/jarvis`; escolha outro local com `--secrets-target`.

**Depois de restaurar:** contas Google podem pedir reconexão, o que é esperado. Vetores são mesclados por ID em um ChromaDB existente.

## Recuperação de desastre

Perdeu o computador? Você precisa de um backup `.jrvb` (de outro disco ou do R2) e da chave de recuperação. Sem a chave, os backups são irrecuperáveis.
