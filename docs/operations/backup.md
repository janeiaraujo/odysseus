# Backup

**Estado: demonstrado com dados reais.**

```bash
scripts/jarvis-backup keygen --out ~/jarvis-recovery.key   # uma vez; copie para fora do computador
scripts/jarvis-backup create --key-file ~/jarvis-recovery.key
scripts/jarvis-backup verify --key-file ~/jarvis-recovery.key ARQUIVO.jrvb
```

Requer um Python com as dependências do projeto (`./venv/bin/python scripts/jarvis-backup ...`).

**Incluído:** `data/` (bancos copiados com segurança, com o app rodando), memórias, skills, configurações, `.env` e vetores do ChromaDB, exportados pela API. **Fora:** caches e modelos baixados, que podem ser obtidos de novo. `--no-secrets` omite chave, autenticação, cofre e `.env`.

**Formato:** AES-256-GCM com chave derivada por scrypt. Adulteração, truncamento e chave errada são detectados. `verify` confere o hash de cada arquivo e a integridade de cada SQLite.

Sem a chave nenhum backup abre, e ninguém consegue recuperá-la.

## ChromaDB

Instalações antigas guardavam vetores fora do volume, e um `docker compose down` os apagaria. `scripts/jarvis-doctor` detecta isso; `scripts/jarvis-migrate-chroma` corrige.

Enviar ao R2: `scripts/jarvis-backup upload`. Retenção: `apply_retention` em `storage/base.py`; ainda não há rotina automática.
