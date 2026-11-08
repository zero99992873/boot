# Paper bot Solana (simulacion en GitHub Actions)

1. Crea un repositorio PUBLICO en github.com (las Actions son gratis e ilimitadas en publicos).
2. Sube bot.py, report.py, requirements.txt y test_offline.py (Add file > Upload files).
3. Crea el workflow: Add file > Create new file, nombre `.github/workflows/paper-bot.yml`,
   y pega el contenido de paper-bot.yml.
4. Settings > Actions > General > Workflow permissions > "Read and write permissions" > Save.
5. Pestana Actions > paper-bot > Run workflow (primera ejecucion manual).
6. Resultados: abre REPORT.txt en el repo (se actualiza solo).

Notas: el repo es publico, asi que bot.db y REPORT.txt lo son; no pongas claves nunca.
GitHub pausa los workflows programados tras 60 dias sin actividad en el repo.
