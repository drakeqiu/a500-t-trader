"""Argument parser composition for the public CLI."""

import argparse

from . import account, history, intraday, planning


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="a500-t-trader")
    commands = parser.add_subparsers(dest="command", required=True)
    planning.register_premarket(commands)
    account.register_commands(commands)
    history.register_commands(commands)
    planning.register_monitor_review(commands)
    intraday.register_commands(commands)
    return parser
