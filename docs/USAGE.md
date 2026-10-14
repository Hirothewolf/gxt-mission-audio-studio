# Uso

1. Abra seu GXT do mod; use o GXT do jogo base apenas como comparação.
2. Configure a instalação do GTA SA, o diretório de trabalho e o mod alvo nos ajustes/fontes.
   O caminho mostrado no cabeçalho identifica o GXT que será salvo. Um GXT dentro do ModLoader
   determina o mod de consulta/aplicação.
3. Escolha uma tabela ou um grupo em Missões e categorias. A busca não muda a identidade das entradas.
4. Duplo clique/F2 edita Texto atual. Enter confirma, Shift+Enter cria uma nova linha e Esc descarta.
   Ctrl+S salva; textos incompatíveis com a codificação GXT não são gravados silenciosamente.
5. F5 ouve o original, F6 o take escolhido/gerado, F8 o mod e F7 compara. Uma fonte presente em banco
   pode exigir preparação do trecho antes da primeira audição.
6. Em Revisar/gerar, ajuste velocidade, duração e seed. `livre` evita impor uma janela fixa à síntese;
   `original` usa a duração associada àquela fala. Seed vazia é aleatória. Ouça e escolha o take desejado.
7. Salvar aplica os takes escolhidos ao mod com as verificações e backups do publicador. Sem take novo,
   o áudio existente permanece. Feche o GTA antes de aplicar bancos.

## Cutscenes e BEATS

Use Cutscenes para selecionar a cena inteira. A timeline mantém fonte original, camadas e seleções.
Um trecho usa a referência original correspondente, não uma voz genérica por personagem.
SCRIPT e streams BEATS/CUTSCENE têm rotas distintas; um trecho de stream não deve ser gravado como slot SCRIPT.
Sobreposições intencionais são possíveis. Salvar um projeto de timeline e aplicar a mistura ao mod são ações distintas.

## NPCs

A aba NPCs lê o catálogo local recuperado e verificado por ID físico SPC. O repositório não fornece
o catálogo de transcrições, WAVs ou checkpoints pessoais. Consulte `studio/audio/npc_catalog.py`
para o contrato de recuperação e `docs/DEPENDENCIES.md` para a estrutura externa.
Transcrição original permanece separada do texto editado. Trocar de linha ou filtrar preserva rascunhos;
uma referência alterada exige salvar explicitamente o vínculo revisado. Idioma/falante não são inferidos de uma missão.
