# Verificação da cópia de publicação

Em 14/10/2026, a cópia limpa deste repositório foi testada separadamente da instalação de trabalho,
com perfil de dados local isolado e sem transportar configuração pessoal:

- 649 testes Qt/backend e 49 subtestes passaram.
- 36 testes da interface clássica passaram em processo separado com Tk completo.
- O pacote wheel foi construído; interface Qt, worker OmniVoice e metadados necessários estavam presentes.
- Não foram detectados valores de credenciais nos formatos verificados, nem caminhos pessoais nos arquivos de texto.
- A distribuição exclui GXT/SCM/IMG, áudios, modelos, runtimes CUDA, SAAT binário, caches e transcrições pessoais.

Ambiente local dos testes Qt: Windows, Python 3.13.13, PySide6 6.11.2, NumPy 2.5.4 e SoundFile 0.14.0.
Os testes clássicos usaram Python 3.14 com Tk completo. Não houve inferência de voz, gasto de GPU cloud
ou instalação de áudio no jogo durante esta verificação.

As capturas vieram de testes Windows da instalação de trabalho: navegação, seleção de lote, consulta
de original/mod/take e catálogo NPC foram exercitados. Capturas e testes automatizados não comprovam
qualidade vocal nem compatibilidade de gameplay em outra instalação. O workflow público é configurado
para repetir os testes no GitHub Actions; seu resultado remoto deve ser verificado separadamente.
