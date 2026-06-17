from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import env, load_env, require_env
from . import settings
from .models import GeneratedMessage, Lead
from .pipeline import filter_new_leads, generate_messages, output_path, processed_file_path, run_lead_job, send_messages
from .storage import (
    append_processed_leads_csv,
    read_leads_csv,
    read_messages_jsonl,
    read_processed_lead_keys,
    write_leads_csv,
    write_messages_jsonl,
)


SEND_CONFIRMATION = "I_UNDERSTAND_THIS_SENDS_EMAIL"


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        print("Команда не указана, запускаю Telegram-бота. Для справки: python3 run.py --help")
    argv = default_argv(raw_argv)
    args = parser.parse_args(argv)

    if args.command == "scrape":
        result = run_lead_job(
            query=args.query,
            location=args.location,
            limit=args.limit,
            output_dir=args.output_dir,
            generate=False,
            headless=not args.headful,
            delay_seconds=args.delay,
            processed_file=args.processed_file,
            log=print,
        )
        print_summary(result.leads, result.leads_path)
        print(f"Processed registry: {result.processed_path}")
        return 0

    if args.command == "generate":
        processed_path = processed_file_path(args.output_dir, args.processed_file)
        processed_keys = read_processed_lead_keys(processed_path)
        leads, skipped_existing = filter_new_leads(
            [lead for lead in read_leads_csv(args.input) if lead.website_absent_verified],
            processed_keys,
        )
        if skipped_existing:
            print(f"Skipped already processed leads: {skipped_existing}")
        messages = generate_messages(leads)
        path = output_path(args.output_dir, "messages", "jsonl")
        write_messages_jsonl(path, messages)
        added_to_registry = append_processed_leads_csv(processed_path, leads)
        print(f"Generated {len(messages)} drafts: {path}")
        print(f"Processed registry: {processed_path} (+{added_to_registry})")
        return 0

    if args.command == "send":
        if not args.send_emails:
            raise SystemExit("Nothing to send: add --send-emails.")
        messages = read_messages_jsonl(args.input)
        sent = send_messages(messages, args.confirm_send, SEND_CONFIRMATION, log=print)
        path = output_path(args.output_dir, "messages_sent", "jsonl")
        write_messages_jsonl(path, sent)
        print(f"Processed {len(sent)} messages: {path}")
        return 0

    if args.command == "run":
        if args.input:
            processed_path = processed_file_path(args.output_dir, args.processed_file)
            processed_keys = read_processed_lead_keys(processed_path)
            leads, skipped_existing = filter_new_leads(read_leads_csv(args.input), processed_keys)
            leads_path = output_path(args.output_dir, "leads", "csv")
            write_leads_csv(leads_path, leads)
            no_site_leads = [lead for lead in leads if lead.website_absent_verified]
            print_summary(leads, leads_path)
            if skipped_existing:
                print(f"Skipped already processed leads: {skipped_existing}")
            print(f"Without website: {len(no_site_leads)}")

            messages: list[GeneratedMessage] = []
            if not args.no_generate:
                messages = generate_messages(no_site_leads, log=print)
                messages_path = output_path(args.output_dir, "messages", "jsonl")
                write_messages_jsonl(messages_path, messages)
                print(f"Generated {len(messages)} drafts: {messages_path}")
            added_to_registry = append_processed_leads_csv(processed_path, leads)
            print(f"Processed registry: {processed_path} (+{added_to_registry})")
        else:
            if not args.query:
                raise SystemExit("Pass --query, or use --input to process an existing CSV.")
            result = run_lead_job(
                query=args.query,
                location=args.location,
                limit=args.limit,
                output_dir=args.output_dir,
                generate=not args.no_generate,
                headless=not args.headful,
                delay_seconds=args.delay,
                processed_file=args.processed_file,
                log=print,
            )
            messages = result.messages

        if args.send_emails:
            sent = send_messages(messages, args.confirm_send, SEND_CONFIRMATION, log=print)
            sent_path = output_path(args.output_dir, "messages_sent", "jsonl")
            write_messages_jsonl(sent_path, sent)
            print(f"Processed {len(sent)} messages: {sent_path}")
        return 0

    if args.command == "bot":
        from .telegram_bot import TelegramLeadBot

        bot = TelegramLeadBot(
            token=require_env("TELEGRAM_BOT_TOKEN"),
            output_dir=args.output_dir,
            default_limit=args.limit,
            default_city=args.location,
            headless=not args.headful,
            delay_seconds=args.delay,
            allowed_chat_ids=parse_allowed_chat_ids(env("TELEGRAM_ALLOWED_CHAT_IDS")),
        )
        bot.run()
        return 0

    parser.print_help()
    return 2


def default_argv(argv: list[str] | None = None) -> list[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        return args
    return ["bot"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Yandex Maps lead parser and outreach generator.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scrape = subparsers.add_parser("scrape", help="Scrape Yandex Maps leads.")
    add_scrape_args(scrape, query_required=True)
    add_output_args(scrape)
    add_processed_args(scrape)

    generate = subparsers.add_parser("generate", help="Generate outreach drafts for leads without websites.")
    generate.add_argument("--input", type=Path, required=True, help="Input leads CSV.")
    add_output_args(generate)
    add_processed_args(generate)

    send = subparsers.add_parser("send", help="Send generated email drafts.")
    send.add_argument("--input", type=Path, required=True, help="Input messages JSONL.")
    send.add_argument("--send-emails", action="store_true", help="Send emails through SMTP.")
    send.add_argument("--confirm-send", default="", help=f"Must equal {SEND_CONFIRMATION}.")
    add_output_args(send)

    run = subparsers.add_parser("run", help="Scrape, filter, generate drafts, and optionally send emails.")
    add_scrape_args(run, query_required=False)
    run.add_argument("--input", type=Path, help="Use an existing leads CSV instead of scraping.")
    run.add_argument("--no-generate", action="store_true", help="Only scrape and filter leads.")
    run.add_argument("--send-emails", action="store_true", help="Send emails through SMTP after generation.")
    run.add_argument("--confirm-send", default="", help=f"Must equal {SEND_CONFIRMATION}.")
    add_output_args(run)
    add_processed_args(run)

    bot = subparsers.add_parser("bot", help="Run Telegram bot for search jobs and progress logs.")
    bot.add_argument("--limit", type=int, default=int(env("TELEGRAM_DEFAULT_LIMIT", "10")), help="Default search limit.")
    bot.add_argument(
        "--location",
        "--city",
        dest="location",
        default=env("TELEGRAM_DEFAULT_CITY", ""),
        help='Default city for Telegram jobs, e.g. "Волгоград".',
    )
    bot.add_argument("--headful", action="store_true", help="Show the browser window.")
    bot.add_argument(
        "--delay",
        type=float,
        default=settings.PAGE_DELAY,
        help="Пауза между карточками организаций (сек). Меньше — быстрее, выше риск капчи.",
    )
    add_output_args(bot)

    return parser


def add_scrape_args(parser: argparse.ArgumentParser, query_required: bool) -> None:
    parser.add_argument("--query", required=query_required, help='Search query, e.g. "кофейни".')
    parser.add_argument("--location", "--city", dest="location", default="", help='City or area, e.g. "Волгоград".')
    parser.add_argument("--limit", type=int, default=20, help="Maximum organizations to inspect.")
    parser.add_argument("--headful", action="store_true", help="Show the browser window.")
    parser.add_argument(
        "--delay",
        type=float,
        default=settings.PAGE_DELAY,
        help="Delay between organization pages.",
    )


def add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", type=Path, default=Path("out"), help="Directory for CSV/JSONL outputs.")


def add_processed_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--processed-file",
        type=Path,
        default=None,
        help="CSV file with already processed organizations. Default: OUTPUT_DIR/processed_leads.csv.",
    )


def print_summary(leads: list[Lead], path: Path) -> None:
    with_site = sum(1 for lead in leads if lead.has_website)
    no_site_verified = sum(1 for lead in leads if lead.website_absent_verified)
    unknown_site = sum(1 for lead in leads if not lead.has_website and not lead.website_absent_verified)
    print(f"Saved {len(leads)} leads: {path}")
    print(
        f"With website: {with_site}; without website verified: {no_site_verified}; "
        f"website unknown: {unknown_site}"
    )


def parse_allowed_chat_ids(value: str) -> set[int]:
    result: set[int] = set()
    for item in value.replace(";", ",").split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return result
