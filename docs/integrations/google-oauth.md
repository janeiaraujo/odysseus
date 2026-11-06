# Google OAuth

**Estado: código testado com simulações; ainda não validado contra o Google real.**

1. Em [console.cloud.google.com](https://console.cloud.google.com), crie um projeto e ative as APIs: Calendar, Gmail, Drive e Photos Picker.
2. Configure a tela de consentimento como **Externo** e adicione seu e-mail como usuário de teste.
3. Crie uma credencial **OAuth client ID** do tipo *Web application*, com os redirects:
   - `http://localhost:7000/api/jarvis/google/callback`
   - `http://localhost:7000/api/jarvis/google/login/callback`
4. Coloque `GOOGLE_OAUTH_CLIENT_ID` e `GOOGLE_OAUTH_CLIENT_SECRET` no `.env` e recrie o container.
5. No painel, aba **Integrações**, marque o que deseja autorizar e conecte.

## O que esperar

- **Consentimento incremental:** só os escopos marcados são pedidos, e é possível ampliar depois.
- **Tokens:** criptografados em repouso, renovados automaticamente e nunca registrados em logs. Se o Google recusar a renovação, a conta fica como "reconectar" e nada tenta contornar isso.
- **Modo de teste do Google:** enquanto o app estiver em *Testing*, o Google expira refresh tokens em 7 dias. Publicar o app com escopos restritos do Gmail exige verificação do Google, normalmente inviável para uso pessoal.
- **Login com Google:** só funciona para contas já vinculadas, nunca cria usuário e é recusado se a conta Jarvis usa 2FA.
- **Revogar:** apaga os tokens locais mesmo se o Google estiver fora do ar.
