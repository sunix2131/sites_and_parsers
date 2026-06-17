from __future__ import annotations

import argparse
from pathlib import Path

from . import settings
from .config import env, load_env
from .link_tools import check_yandex_org_links, collect_yandex_org_links
from .models import Lead
from .pipeline import run_lead_job
from .run_modes import RUN_MODES, apply_run_profile


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    profile = apply_run_profile(args.profile)
    delay = args.delay if args.delay is not None else profile.page_delay

    if args.command == "collect-links":
        result = collect_yandex_org_links(
            query=args.query,
            location=args.location,
            limit=args.limit,
            output_dir=args.output_dir,
            headless=not args.headful,
            log=print,
        )
        print(f"Profile: {profile.key} ({profile.label})")
        print(f"Collected org links: {len(result.urls)}")
        print(f"URLs CSV: {result.urls_path}")
        return 0

    if args.command == "check-links":
        result = check_yandex_org_links(
            input_path=args.input,
            output_dir=args.output_dir,
            processed_file=args.processed_file,
            headless=not args.headful,
            delay_seconds=delay,
            contact_filter=args.contacts,
            only_no_site=args.only_no_site,
            log=print,
        )
        print(f"Profile: {profile.key} ({profile.label})")
        print(f"Checked: {result.checked}; skipped existing: {result.skipped_existing}; failed: {result.failed}")
        print_summary(result.leads, result.leads_path)
        print(f"Processed registry: {result.processed_path}")
        return 0

    if args.command == "scrape-fast":
        result = run_lead_job(
            query=args.query,
            location=args.location,
            limit=args.limit,
            output_dir=args.output_dir,
            generate=False,
            headless=not args.headful,
            delay_seconds=delay,
            processed_file=args.processed_file,
            log=print,
            light_parse=args.light,
            prefer_no_site_stop=args.prefer_no_site_stop,
            contact_filter=args.contacts,
        )
        print(f"Profile: {profile.key} ({profile.label})")
        print_summary(result.leads, result.leads_path)
        print(f"Processed registry: {result.processed_path}")
        return 0

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fast workflow helpers for Maps Parser.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect-links", help="Collect /org/ links from Yandex Maps without checking cards.")
    add_search_args(collect)
    add_common_args(collect)

    check = subparsers.add_parser("check-links", help="Check saved /org/ links from CSV or text file.")
    check.add_argument("--input", type=Path, required=True, help="CSV/text file with Yandex Maps /org/ URLs.")
    check.add_argument("--only-no-site", action="store_true", help="Save only verified no-site leads.")
    check.add_argument("--processed-file", type=Path, default=None, help="Processed registry CSV.")
    add_contacts_arg(check)
    add_common_args(check)

    scrape = subparsers.add_parser("scrape-fast", help="Run existing scraper with selectable speed profile.")
    add_search_args(scrape)
    scrape.add_argument("--processed-file", type=Path, default=None, help="Processed registry CSV.")
    scrape.add_argument("--light", action="store_true", help="Use faster light parsing where supported.")
    scrape.add_argument("--prefer-no-site-stop", action="store_true", help="Treat --limit as target no-site lead count.")
    add_contacts_arg(scrape)
    add_common_args(scrape)

    return parser


def add_search_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query", required=True, help='Search query, e.g. "кофейни".')
    parser.add_argument("--location", "--city", dest="location", default="", help='City or area, e.g. "Волгоград".')
    parser.add_argument("--limit", type=int, default=20, help="Target count for this command.")


def add_contacts_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--contacts",
        choices=("phone", "any", "all"),
        default="all",
        help="Contact filter: phone only, any contact, or all leads.",
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--headful", action="store_true", help="Show the browser window.")
    parser.add_argument("--output-dir", type=Path, default=Path("out"), help="Directory for outputs.")
    parser.add_argument("--delay", type=float, default=None, help="Delay between checked cards. Default comes from profile.")
    parser.add_argument(
        "--profile",
        choices=tuple(RUN_MODES),
        default=env("PARSER_PROFILE", settings.PARSER_PROFILE),
        help="Parser speed profile.",
    )


def print_summary(leads: list[Lead], path: Path) -> None:
    with_site = sum(1 for lead in leads if lead.has_website)
    no_site_verified = sum(1 for lead in leads if lead.website_absent_verified)
    unknown_site = sum(1 for lead in leads if not lead.has_website and not lead.website_absent_verified)
    with_phone = sum(1 for lead in leads if lead.has_phone)
    with_any_contact = sum(1 for lead in leads if lead.has_any_contact)
    print(f"Saved {len(leads)} leads: {path}")
    print(
        f"With website: {with_site}; without website verified: {no_site_verified}; "
        f"website unknown: {unknown_site}; with phone: {with_phone}; with any contact: {with_any_contact}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
