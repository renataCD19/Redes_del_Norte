name: Actualizar datos

on:
  schedule:
    - cron: "0 13 * * *"   # todos los días a las 13:00 UTC (7:00 am en Monterrey)
  workflow_dispatch:        # permite ejecutarlo a mano desde la pestaña Actions

permissions:
  contents: write

concurrency:
  group: actualizar-datos
  cancel-in-progress: false

jobs:
  actualizar:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Instalar dependencias
        run: pip install -r requirements.txt

      - name: Recolectar y clasificar
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: python scripts/actualizar.py

      - name: Guardar data.json
        run: |
          git config user.name "radar-bot"
          git config user.email "radar-bot@users.noreply.github.com"
          git add data.json
          git diff --staged --quiet || git commit -m "Actualizar datos $(date -u +%F)"
          git push
