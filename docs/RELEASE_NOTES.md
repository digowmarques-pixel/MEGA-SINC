## Mega Sinc 1.2.0

Sincronização automática de múltiplas câmeras e gravadores pelo áudio.

### Downloads
| Arquivo | Para |
|---|---|
| `MegaSinc-macOS-arm64.dmg` | Mac M1, M2, M3, M4 |
| `MegaSinc-macOS-x86_64.dmg` | Mac Intel |
| `MegaSinc-Setup-1.2.0.exe` | Windows (instalador) |
| `MegaSinc-Windows-x64-portable.zip` | Windows (sem instalar) |

### Primeira abertura
- **Mac:** abra o `.dmg`, arraste o Mega Sinc para Aplicativos e, na primeira vez, clique com o botão direito no app → **Abrir** → **Abrir**.
- **Windows:** se o SmartScreen avisar, clique em **Mais informações → Executar assim mesmo**.

### Novidades da 1.2
- **Duas ou mais câmeras sem gravador** (muitos clipes curtos): usa o horário de gravação de cada câmera para descartar correspondências falsas (ex.: música repetitiva), procurar clipes difíceis perto da posição prevista e posicionar pares/grupos isolados — sem sobreposições impossíveis na mesma câmera
- Clipes sem áudio em comum com nenhuma outra câmera ficam **listrados**, posicionados pelo horário
- Avisos vão para o painel Detalhes (sem janelas travando a tela)
- Faixas com prefixo curto (ex.: "P1458…") recebem o nome da pasta (ex.: "CAM 01")

### Novidades da 1.1
- **Player**: ▶ Ouvir (barra de espaço) toca a timeline sincronizada; M (mudo) e S (solo) por faixa
- **Ajuste manual**: arraste clipes na horizontal, posicione os vermelhos à mão, ← → move 1 quadro, Ctrl+Z desfaz
- **Timecode**: sincronizar só por timecode ou áudio + timecode
- **Canal de análise** por faixa (ex.: lapela no canal 2)
- **Multicâmera**: clipe multicâmera para Final Cut Pro e DaVinci Resolve; opção de silenciar o áudio das câmeras

### Recursos
- Agrupamento automático por câmera/gravador
- Sincronização com precisão abaixo de 1 ms e detecção/correção de drift
- Exporta XML para Premiere Pro, FCPXML para Final Cut Pro e DaVinci Resolve
- Gera vídeos com o áudio do gravador já sincronizado
- Clipes que não sincronizam ficam destacados em vermelho
