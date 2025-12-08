#!/usr/bin/env python3
"""Test script for GENAI upload endpoint.

Usage examples:
  python scripts/test_genai_upload.py --url http://127.0.0.1:8000 --files sample.csv sample.pdf --query "Summarize the data" --summarize

The script posts files to POST /api/genai/query/upload and prints the JSON response.
"""
import argparse
import os
import sys
import requests


def make_sample_csv(path: str):
    rows = [
        ["id", "name", "value", "timestamp"],
        ["1", "alpha", "10.5", "2023-01-01"],
        ["2", "beta", "20", "2023-02-01"],
        ["3", "gamma", "", "2023-03-01"],
    ]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(",".join(r) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Test /api/genai/query/upload endpoint")
    parser.add_argument("--url", default=os.getenv("GENAI_TEST_URL", "http://127.0.0.1:8000"), help="Base URL of the server")
    parser.add_argument("--files", nargs="*", help="Files to upload")
    parser.add_argument("--query", default="Please analyze the uploaded documents.", help="Text query to send")
    parser.add_argument("--top_k", type=int, default=4)
    parser.add_argument("--summarize", action="store_true", help="Ask the server to summarize CSVs locally before sending to LLM")

    args = parser.parse_args()

    files = args.files or []

    # If no files provided, generate a small sample CSV and use it
    generated = []
    if not files:
        sample = os.path.join(os.getcwd(), "sample_genai.csv")
        make_sample_csv(sample)
        files = [sample]
        generated.append(sample)

    multipart = []
    opened_files = []
    try:
        for p in files:
            if not os.path.exists(p):
                print(f"File not found: {p}", file=sys.stderr)
                sys.exit(2)
            f = open(p, "rb")
            opened_files.append(f)
            multipart.append(("files", (os.path.basename(p), f, "application/octet-stream")))

        data = {"query": args.query, "top_k": str(args.top_k)}
        if args.summarize:
            data["summarize"] = "true"

        url = args.url.rstrip("/") + "/api/genai/query/upload"
        print(f"POST {url} with files={files} summarize={args.summarize}")
        resp = requests.post(url, files=multipart, data=data, timeout=60)
        print("Status:", resp.status_code)
        try:
            print(resp.json())
        except Exception:
            print(resp.text)

    finally:
        for f in opened_files:
            try:
                f.close()
            except Exception:
                pass

        # cleanup generated sample
        for p in generated:
            try:
                os.remove(p)
            except Exception:
                pass


if __name__ == "__main__":
    main()
