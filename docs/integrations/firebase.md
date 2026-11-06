# Firebase

**Estado: projeto criado e banco ativo, com acesso anônimo confirmado como negado. Código validado no emulador oficial. A primeira gravação real depende da chave chegar a este servidor. FCM não testado.**

## O que ele faz aqui

O SQLite continua sendo a fonte da verdade. O Firestore recebe uma cópia de memórias e dispositivos por uma fila persistente. Se o Firebase cair, nada no Jarvis para e a fila reenvia depois. Veja por que não substituí o SQLite na [decisão D1](../architecture/decisions.md).

O caminho dos documentos usa um hash em vez do seu usuário, e o texto das memórias sai criptografado por padrão (`firebase.encrypt_payloads`). Um cliente Android só conseguirá ler esse texto quando houver um mecanismo de troca de chave, então essa escolha precisará ser revista junto com o app.

## Ativar

1. Em [console.firebase.google.com](https://console.firebase.google.com), crie um projeto no plano Spark e ative o Firestore em **modo de produção**. Backups do próprio Firestore exigem o plano pago Blaze; os do Jarvis os dispensam.
2. Em *Configurações > Contas de serviço*, gere uma chave e guarde-a como `~/.config/jarvis/firebase-sa.json`, com `chmod 600`.
3. Valide: `./venv/bin/python scripts/jarvis-firebase-check --project SEU_PROJETO`.
4. No `.env`:
   ```
   JARVIS_SECRETS_DIR=/home/voce/.config/jarvis
   JARVIS_FIREBASE_PROJECT=SEU_PROJETO
   JARVIS_FIREBASE_CREDENTIALS=/app/secrets/firebase-sa.json
   JARVIS_FIREBASE_ENABLED=true
   ```
5. Recrie o container. O diretório entra somente leitura em `/app/secrets`, e a chave vai no backup criptografado.

A localização do banco não pode ser alterada depois de criada. Revogue a chave em *Contas de serviço* se ela vazar.

## Testar com o emulador

```bash
npx firebase-tools@13 emulators:exec --only firestore --project demo-jarvis \
  "FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 python -m pytest tests/jarvis/test_firebase.py -k emulator"
```

Requer Java e um `firebase.json` configurando a porta.
