#!/usr/bin/env python3
"""
PRAgent - Google News scraping agent.
Usage: python main.py --keyword XREAL --pages 30 --output xreal_news.csv
"""
import argparse
import asyncio
import os
import sys

# Ensure src is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.agent import GoogleNewsAgent


def parse_args():
    p = argparse.ArgumentParser(description="PRAgent - Google News scraping")
    p.add_argument("--keyword", type=str, default="XREAL", help="Search keyword")
    p.add_argument("--pages", type=int, default=30, help="Number of pages to scrape")
    p.add_argument("--output", type=str, default="news_output.csv", help="Output CSV/SQLite path")
    p.add_argument("--headless", action="store_true", help="Run browser headless")
    return p.parse_args()


def main():
    args = parse_args()
    try:
        agent = GoogleNewsAgent(keyword=args.keyword, pages=args.pages, output_path=args.output)
        asyncio.run(agent.run(headless=args.headless))
    except ValueError as e:
        if "DASHSCOPE_API_KEY" in str(e):
            print("Error: Set DASHSCOPE_API_KEY environment variable or api_key in config.yaml")
            print("  Example: set DASHSCOPE_API_KEY=your_key")
        else:
            print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
