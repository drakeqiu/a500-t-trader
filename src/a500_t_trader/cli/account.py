"""Account ledger command registration and execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from decimal import Decimal

from ..account_ledger import AccountLedger, import_galaxy_fills
from ..strategy.premarket import load_account


def register_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    account_record = commands.add_parser(
        "account-snapshot", help="persist a user-entered account snapshot"
    )
    account_record.add_argument(
        "--account", required=True, help="user-entered account snapshot JSON"
    )
    account_record.add_argument(
        "--database",
        default=os.environ.get("DATABASE_URL"),
        required=not bool(os.environ.get("DATABASE_URL")),
        help="PostgreSQL connection URL",
    )
    fill_record = commands.add_parser(
        "manual-fill", help="record a user-entered manual fill"
    )
    fill_record.add_argument("--timestamp", required=True, type=datetime.fromisoformat)
    fill_record.add_argument("--symbol", required=True)
    fill_record.add_argument("--side", required=True, choices=("buy", "sell"))
    fill_record.add_argument("--quantity", required=True, type=int)
    fill_record.add_argument("--price", required=True, type=Decimal)
    fill_record.add_argument("--notes", default="")
    fill_record.add_argument(
        "--database",
        default=os.environ.get("DATABASE_URL"),
        required=not bool(os.environ.get("DATABASE_URL")),
        help="PostgreSQL connection URL",
    )
    fill_import = commands.add_parser(
        "import-fills", help="preview or import Galaxy Securities CSV fills"
    )
    fill_import.add_argument("--file", required=True, help="GB18030 Galaxy CSV file")
    fill_import.add_argument(
        "--database",
        default=os.environ.get("DATABASE_URL"),
        required=not bool(os.environ.get("DATABASE_URL")),
        help="PostgreSQL connection URL",
    )
    fill_import.add_argument(
        "--confirm", action="store_true", help="persist valid new imported fills"
    )
    ledger_show = commands.add_parser(
        "account-ledger", help="show saved account snapshots and fills"
    )
    ledger_show.add_argument(
        "--database",
        default=os.environ.get("DATABASE_URL"),
        required=not bool(os.environ.get("DATABASE_URL")),
        help="PostgreSQL connection URL",
    )


def handle(args: argparse.Namespace) -> int:
    if args.command == "import-fills":
        try:
            report = import_galaxy_fills(args.database, args.file, confirm=args.confirm)
        except Exception:
            print(
                "Unable to import Galaxy fills: ledger operation failed.",
                file=sys.stderr,
            )
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if report["errors"] else 0
    if args.command == "account-snapshot":
        try:
            identifier = AccountLedger(args.database).record_snapshot(
                load_account(args.account)
            )
        except Exception as error:
            print(f"Unable to record account snapshot: {error}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "id": identifier,
                    "classification": "user-entered; not broker-verified",
                }
            )
        )
        return 0
    if args.command == "manual-fill":
        try:
            identifier = AccountLedger(args.database).record_fill(
                args.timestamp,
                args.symbol,
                args.side,
                args.quantity,
                args.price,
                args.notes,
            )
        except Exception as error:
            print(f"Unable to record manual fill: {error}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "id": identifier,
                    "classification": "user-entered manual fill; not broker-verified",
                }
            )
        )
        return 0
    ledger = AccountLedger(args.database)
    print(
        json.dumps(
            {
                "snapshots": ledger.snapshots(),
                "manual_fills": ledger.fills(),
                "imported_fills": ledger.imported_fills(),
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )
    return 0


def _json_default(value: object) -> str:
    from .common import json_default

    return json_default(value)
