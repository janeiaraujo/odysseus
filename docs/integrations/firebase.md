# Firebase

**Estado: Firestore validado no emulador oficial. Não conectado a um projeto real, pois esta máquina não tem login no Firebase. FCM não testado.**

## O que ele faz aqui

O SQLite continua sendo a fonte da verdade. O Firestore recebe uma cópia de memórias e dispositivos por uma fila persistente. Se o Firebase cair, nada no Jarvis para e a fila reenvia depois. Veja por que não substituí o SQLite na [decisão D1](../architecture/decisions.md).

O caminho dos documentos usa um hash em vez do seu usuário, e o texto das memórias sai criptografado por padrão (`firebase.encrypt_payloads`). Um cliente Android só conseguirá ler esse texto quando houver um mecanismo de troca de chave, então essa escolha precisará ser revista junto com o app.

## Ativar

1. Em [console.firebase.google.com](https://console.firebase.google.com), crie um projeto e ative o Firestore.
2. Em *Contas de serviço*, gere uma chave e salve o JSON **fora do repositório**.
3. No `.env`: `JARVIS_FIREBASE_CREDENTIALS=/caminho/chave.json` (montado no container).
4. `PATCH /api/jarvis/config` com `{"firebase": {"enabled": true, "project_id": "seu-projeto"}}`.

Ative regras de segurança que neguem acesso público: a conta de serviço as ignora, e nenhum cliente precisa de acesso direto por enquanto.

## Testar com o emulador

```bash
npx firebase-tools@13 emulators:exec --only firestore --project demo-jarvis \
  "FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 python -m pytest tests/jarvis/test_firebase.py -k emulator"
```

Requer Java e um `firebase.json` configurando a porta.
