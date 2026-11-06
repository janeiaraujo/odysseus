# Segredos

| Segredo | Onde fica |
|---|---|
| Chaves de API, tokens OAuth, senhas | Criptografados no banco, com `data/.app_key` |
| Chave de recuperação | `~/jarvis-recovery.key`, fora do repositório |
| Conta de serviço do Firebase | Arquivo fora do repositório |

`.env`, `data/`, `backups/`, `*.jrvb` e `*recovery.key` estão no `.gitignore`. Este fork é **público**: `scripts/jarvis-doctor` avisa se algo privado for versionado. Logs e auditoria passam por `redact.py`.

Perder `data/.app_key` torna ilegíveis os segredos salvos. Ela vai no backup, protegida pela chave de recuperação.

**Se vazar:** revogue no provedor, troque no painel e faça novo backup. Reescrever o histórico do Git não basta em um repositório público.
