# Configuração

Tudo é opcional. Sem nenhuma variável, o Jarvis roda com memória, comandos, missões, roteador e backup local.

| Variável | Uso |
|---|---|
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | Integrações Google |
| `JARVIS_PUBLIC_URL` | Base dos redirects OAuth, atrás de proxy |
| `JARVIS_R2_ACCOUNT_ID`, `JARVIS_R2_BUCKET`, `JARVIS_R2_ACCESS_KEY_ID`, `JARVIS_R2_SECRET_ACCESS_KEY` | Cloudflare R2 |
| `JARVIS_R2_ENDPOINT` | Outro servidor compatível com S3 |
| `JARVIS_FIREBASE_PROJECT`, `JARVIS_FIREBASE_CREDENTIALS` | Firestore e FCM |
| `FIRESTORE_EMULATOR_HOST` | Usar o emulador oficial |
| `JARVIS_BROWSER_WS`, `JARVIS_BROWSER_CHANNEL` | Navegador isolado ou local |
| `JARVIS_BACKUP_KEY`, `JARVIS_BACKUP_KEY_FILE`, `JARVIS_BACKUP_DIR`, `APP_BACKUP_DIR` | Backup pela interface |
| `JARVIS_DATABASE_URL` | Outro banco para o Jarvis |
| `ODYSSEUS_INPROCESS_TASKS=0` | Desliga o agendador e o worker do Jarvis neste processo |

As demais opções ficam no painel, aba **Ajustes**, e em `data/jarvis/config.json`.
