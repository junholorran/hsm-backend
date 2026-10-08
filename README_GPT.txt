INSTRUCAO PRO GPT COM ACESSO:
1. Repo junholorran/hsm-backend branch exp-poi-lifecycle-abc-sol
2. Base funcional: commit 4d93eebd93de61ebea2e17eaddd0134f7b238f89
3. Aplicar apenas infraestrutura/telemetria desta pasta; nao alterar a matematica causal validada.
4. railway.json usa Railpack atual, roda os testes no preDeploy e inicia python -m app.
5. Railway vars: SCANNER_INTERVAL=15 WORKERS=6 TIMEZONE_CALC=UTC PAIRS=13 pares.
6. Validar log: scanner ENABLED pairs=13 interval=15s workers=6 + testes passando.
7. WHY_CODES e normalizacao de instrumento sao auxiliares; nao sao gates de direcao.
