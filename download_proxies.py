"""
Standalone: download premium proxies for an existing account token.

Usage:
  python download_proxies.py
      # uses last line in keys/proxyscrape_accounts.txt
  python download_proxies.py --token JWT --account-id UUID
  python download_proxies.py --from-file keys/proxyscrape_accounts.txt

Output:
  keys/proxies.txt  (append)
  keys/proxies_{accountId}.txt
  lines: protocol://user:pass@host:port
"""
import argparse
import os
import sys

from dotenv import load_dotenv
from curl_cffi import requests

load_dotenv()

from src.proxyscrape_helpers import (  # noqa: E402
    DEFAULT_PROXY_PROTOCOL,
    pick_subaccount_id,
)
from main import (  # noqa: E402
    download_premium_proxies,
    fetch_account_me,
    fetch_overview,
    create_session,
)
from src.proxyscrape_helpers import save_proxy_lines, proxy_list_page_url  # noqa: E402


def _parse_account_line(line: str):
    parts = line.strip().split("----")
    if len(parts) < 3:
        return None, None, None
    return parts[0], parts[1], parts[2]


def main():
    ap = argparse.ArgumentParser(description="Download ProxyScrape premium proxies")
    ap.add_argument("--token", help="Bearer access token")
    ap.add_argument("--account-id", help="Subaccount AccountID")
    ap.add_argument(
        "--from-file",
        default="keys/proxyscrape_accounts.txt",
        help="email----password----token file (default last line)",
    )
    ap.add_argument(
        "--protocol",
        default=os.getenv("PROXY_DOWNLOAD_PROTOCOL", DEFAULT_PROXY_PROTOCOL),
        help="http (trial) or socks5 if plan allows",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="Download for every account line in --from-file",
    )
    args = ap.parse_args()

    jobs = []
    if args.token:
        jobs.append(("", args.token, args.account_id))
    else:
        path = args.from_file
        if not os.path.isfile(path):
            print(f"[-] file not found: {path}")
            sys.exit(1)
        lines = [
            ln
            for ln in open(path, encoding="utf-8")
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not lines:
            print("[-] no accounts in file")
            sys.exit(1)
        if args.all:
            for ln in lines:
                e, p, t = _parse_account_line(ln)
                if t:
                    jobs.append((e, t, None))
        else:
            e, p, t = _parse_account_line(lines[-1])
            jobs.append((e, t, args.account_id))

    total = 0
    with create_session() as session:
        for email, token, account_id in jobs:
            me = fetch_account_me(session, token)
            if not me:
                print(f"[-] {email or token[:16]} /me failed")
                continue
            aid = account_id or pick_subaccount_id(me)
            if not aid:
                print(f"[-] {email} no AccountID")
                continue
            print(f"[*] {email or aid} page={proxy_list_page_url(aid)}")
            ov = fetch_overview(session, token, aid)
            if ov and isinstance(ov.get("data"), dict):
                dc = (ov["data"].get("services") or {}).get("datacenter_shared") or {}
                print(
                    f"[*] user={dc.get('proxy_username')} "
                    f"amount={dc.get('proxy_amount')}"
                )
            dres = download_premium_proxies(session, token, aid, args.protocol)
            if not dres["ok"]:
                print(f"[-] download failed: {dres['message']}")
                continue
            paths = save_proxy_lines(
                dres["lines"], account_id=aid, email=email or ""
            )
            total += paths["count"]
            print(
                f"[✓] {paths['count']} lines -> {paths['proxies']} "
                f"sample={dres['sample']}"
            )

    print(f"[*] done, total lines this run: {total}")


if __name__ == "__main__":
    main()
