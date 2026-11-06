# Instalação com Docker

```bash
git clone https://github.com/janeiaraujo/odysseus.git jarvis
cd jarvis
git checkout jarvis/dev
scripts/jarvis-install
```

O script não sobrescreve `.env` nem chave existente. Ele cria `~/jarvis-recovery.key` se não houver uma: **guarde uma cópia fora do computador**.

Clonar recupera o código. O estado (memórias, sessões, tokens) vem de um backup: veja [restauração](../operations/restore.md).

## Instalações anteriores a esta branch

Rode uma vez antes do próximo `docker compose up`, ou os vetores do ChromaDB podem se perder:

```bash
scripts/jarvis-migrate-chroma --dry-run
scripts/jarvis-migrate-chroma
```

## Atualizar

```bash
scripts/jarvis-update --key-file ~/jarvis-recovery.key
```

Faz backup, atualiza somente a partir do seu fork (`origin`), recompila e roda o diagnóstico.
