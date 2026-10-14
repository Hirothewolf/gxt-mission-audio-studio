# Desenvolvimento

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:QT_QPA_PLATFORM="offscreen"
.\.venv\Scripts\python.exe -m pytest tests -q --ignore=tests/test_batch_audition_ui.py --ignore=tests/test_category_navigation_ui.py --ignore=tests/test_bulk_audio_review_ui.py --ignore=tests/test_cutscene_batch_ui.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*_ui.py" -q
```

Os testes Tk devem usar um Python com Tk completo e rodar separadamente da suíte Qt.
Testes usam fixtures temporárias; testes automatizados não comprovam qualidade vocal nem gameplay.

## Organização

- `studio/gxt`: leitura, codificação, validação e gravação GXT.
- `studio/script`: vínculos de SCRIPT, SCM, referências e índices nativos.
- `studio/audio`, `studio/saat`: bancos, streams, análise, timeline e publicação.
- `studio/projects`: takes, aprovação, histórico e persistência de revisão.
- `studio/ui_qt`: interface principal Qt; `studio/ui`: interface clássica Tkinter.
- `studio/tts`: contratos/adaptadores; `tools/omnivoice_tts.py`: worker OmniVoice.
- `studio/data`: metadados offline e fingerprints, sem áudios/texto GXT completo.
- `tests`: testes de backend, persistência e interfaces.

Preserve chaves, coordenadas físicas, origem/hashes e os dados do usuário. Não trate aprovação,
presença em banco e igualdade ao take como o mesmo estado. Alterações na distribuição devem ser
feitas em cópias revisáveis, sem transportar caches, credenciais ou arquivos do jogo.
