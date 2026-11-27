"""Billing operator commands: `stripe-setup`, `stripe-verify`, `stripe-resync`."""
from __future__ import annotations

import argparse
import sys

from ..config import get_settings
from ..db import Database
from . import processor, reconcile, setup
from .stripe_client import StripeError, default_client


def _database(settings) -> Database:
    settings.control_db_path.parent.mkdir(parents=True, exist_ok=True)
    return Database(settings.control_db_path)


def _setup(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = _database(settings)
    try:
        with default_client(settings) as client:
            report = setup.run_setup(db, settings, client)
    except StripeError as exc:
        print(f"stripe-setup failed: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()
    for line in report.created:
        print(f"created   {line}")
    for line in report.reused:
        print(f"existing  {line}")
    for line in report.warnings:
        print(f"warning   {line}")
    if report.env:
        print("\nSet these environment values for the gateway:")
        for key, value in report.env.items():
            print(f"{key}={value}")
    for line in report.problems:
        print(f"PROBLEM   {line}", file=sys.stderr)
    return 1 if report.problems else 0


def _verify(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = _database(settings)
    try:
        with default_client(settings) as client:
            report = setup.verify(db, settings, client)
    except StripeError as exc:
        print(f"stripe-verify failed: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()
    for line in report.warnings:
        print(f"warning   {line}")
    if report.problems:
        for line in report.problems:
            print(f"MISMATCH  {line}", file=sys.stderr)
        return 1
    print(f"stripe-verify: OK (account {report.account_id or 'not readable'}, mode {settings.stripe_mode}, "
          f"prices {', '.join(f'{code}={price}' for code, price in report.prices.items())})")
    return 0


def _resync(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = _database(settings)
    try:
        with default_client(settings) as client:
            recovered = reconcile.reconcile_events(db, settings, client)
            summary = reconcile.full_resync(db, settings, client)
            processed = processor.process_pending(db, settings, client)
    except StripeError as exc:
        print(f"stripe-resync failed: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()
    print(f"events recovered: {recovered}; resync: {summary}; events processed: {processed}")
    return 1 if processed.get("failed") else 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("stripe-setup", help="find or create the Stripe prices and portal configurations (idempotent)")
    parser.set_defaults(func=_setup)
    parser = subparsers.add_parser("stripe-verify", help="compare Stripe prices, account and mode with plans.toml and settings")
    parser.set_defaults(func=_verify)
    parser = subparsers.add_parser("stripe-resync", help="recover missed events, resync every customer and process pending events")
    parser.set_defaults(func=_resync)
