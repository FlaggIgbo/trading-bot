#!/usr/bin/env python3
"""
Price Ladder & Timing Matrix CLI Analysis Tool
==============================================

Empirical modeling tool to backtest and simulate entering prediction markets
at any price X (1¢–99¢) at any time T (minute marks) using historical MongoDB Atlas snapshots.

Usage:
------
# Run 15-minute price matrix modeling for all crypto assets:
python scripts/model_price_patterns.py --timeframe 15m

# Run modeling for Bitcoin only with 1¢ granularity:
python scripts/model_price_patterns.py --symbol BTC --min-price 1 --max-price 99 --step 1

# Run hourly market modeling and export to JSON & CSV:
python scripts/model_price_patterns.py --timeframe 1h --json-output matrix_1h.json --csv-output matrix_1h.csv

# Run Taker fill simulation instead of resting Maker limit bids:
python scripts/model_price_patterns.py --fill-mode taker
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import run_price_ladder_modeling_from_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("price_ladder_cli")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Model hypothetical entries at price X (1¢-99¢) at time T across prediction market lifecycles."
    )
    parser.add_argument(
        "--timeframe", "-t",
        choices=["15m", "1h"],
        default="15m",
        help="Market timeframe to model ('15m' or '1h', default: 15m)",
    )
    parser.add_argument(
        "--symbol", "-s",
        type=str,
        default=None,
        help="Specific asset symbol to analyze (e.g. BTC, ETH, SOL, SPX, default: all assets)",
    )
    parser.add_argument(
        "--min-price",
        type=int,
        default=1,
        help="Minimum price in cents to simulate (default: 1)",
    )
    parser.add_argument(
        "--max-price",
        type=int,
        default=99,
        help="Maximum price in cents to simulate (default: 99)",
    )
    parser.add_argument(
        "--step",
        type=int,
        default=1,
        help="Price increment in cents (default: 1)",
    )
    parser.add_argument(
        "--fill-mode",
        choices=["maker", "taker"],
        default="maker",
        help="Fill simulation mode ('maker' resting bid or 'taker' immediate fill, default: maker)",
    )
    parser.add_argument(
        "--json-output",
        type=str,
        default=None,
        help="Optional filepath to save full JSON matrix",
    )
    parser.add_argument(
        "--csv-output",
        type=str,
        default=None,
        help="Optional filepath to save full CSV matrix",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    log.info(
        f"Starting Price Ladder Modeling: Timeframe={args.timeframe.upper()} | "
        f"Symbol={args.symbol or 'ALL'} | Price Range={args.min_price}¢–{args.max_price}¢ (Step={args.step}¢) | "
        f"Fill Mode={args.fill_mode.upper()}"
    )

    results = run_price_ladder_modeling_from_db(
        timeframe=args.timeframe,
        symbol=args.symbol,
        min_price_cents=args.min_price,
        max_price_cents=args.max_price,
        price_step=args.step,
        fill_mode=args.fill_mode,
        json_output_path=args.json_output,
        csv_output_path=args.csv_output,
    )

    if "report" in results:
        print("\n" + results["report"] + "\n")

    if "markets_analyzed" in results:
        log.info(f"Analysis complete. Markets Analyzed: {results['markets_analyzed']}")


if __name__ == "__main__":
    main()
