# Dependências e arquivos externos

O pacote básico exige Python, PySide6, NumPy, SoundFile e SciPy. A edição clássica também precisa do Tkinter
da instalação oficial do Python. FFmpeg é externo para filtros/encoders e alguns fluxos de streams;
SAAT (`saat_sfx`/`saat_stream`) é externo para extração/importação compatível. Configure os executáveis
localmente; nenhuma DLL ou EXE do SAAT foi redistribuída.

As pastas de trabalho são privadas e ignoradas pelo Git: `workspace/`, `reports/`, `manifests/`,
`audio/`, `.cache/`, `models/`, `backups/` e `deliverables/`. Pesos/runtimes TTS e catálogos de áudio
são configurados na raiz de projeto escolhida (`GXT_PROJECT_ROOT` ou ajustes do editor).

GXT simples funciona sem manifestos do projeto de demonstração. Recursos avançados exigem seus índices:
`manifests/script_mapping_v3.jsonl` para vínculos SCRIPT, índices nativos/alinhamentos para streams,
e recuperação NPC com manifesto, hashes, relatório e WAVs originais correspondentes.
Na ausência desses dados, o recurso deve informar indisponibilidade; não atribui IDs ou vozes fictícias.

OmniVoice é opcional e usa um ambiente próprio com PyTorch compatível com sua GPU e modelo instalado.
Configure o Python dessa instalação na janela de modelos. Qwen/Chatterbox legados dependem de seus
adaptadores externos; o editor não baixa um ambiente CUDA ao abrir. F5/Piper podem aparecer no catálogo,
mas isso não anuncia um adaptador de síntese validado. Hipertradução usa o serviço/provedor configurado;
credenciais locais nunca devem ser versionadas.
