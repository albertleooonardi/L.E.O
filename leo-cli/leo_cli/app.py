"""The `leo` command."""
import argparse
import sys

from . import __version__


def parser():
    p = argparse.ArgumentParser(prog="leo", description="L.E.O CLI: the multi-agent LLM Efficiency Orchestrator in your terminal.")
    p.add_argument("--version", action="version", version=f"leo {__version__}")
    return p


def main(argv=None):
    parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
