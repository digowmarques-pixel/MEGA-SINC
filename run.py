"""Ponto de entrada do aplicativo (usado também pelo PyInstaller).

Sem argumentos (ou com arquivos) abre a interface gráfica; com um subcomando
(`sync`, `info`, `--help`) funciona como linha de comando.
"""
import sys

CLI_COMMANDS = {"sync", "info", "gui", "--version", "-h", "--help"}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-psn_")]  # macOS Finder
    if args and args[0] in CLI_COMMANDS:
        from megasinc.cli import main as cli_main
        return cli_main(args)
    sys.argv = [sys.argv[0], *args]
    from megasinc.gui.app import run_gui
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
