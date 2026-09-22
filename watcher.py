#!/usr/bin/env python3
"""Watch web pages for changes and email a diff when they change.

Written for pages that have no feed and no API: a venue calendar, a ticket
listing, a registration page. Each run fetches the configured sources, reduces
each page to the lines a reader would care about, compares them with the last
run and sends one email describing what changed. When nothing changed it sends
nothing, which is what makes it usable at a 5 or 15 minute interval.

Standard library only, so it runs on a minimal container with python3 and
nothing else installed. Mail goes out over SMTP; the README explains how to use
Amazon SES without an AWS SDK.

Usage:
    watcher.py [--config PATH] [--state PATH] [--dry-run] [--show]
"""

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

DEFAULT_CONFIG = "/etc/watcher/config.json"
DEFAULT_STATE = "/var/lib/watcher/state.json"
DEFAULT_UA = "calendar-change-watcher/1.0 (+https://github.com/defenestratexp/calendar-change-watcher)"
TIMEOUT = 30
STALE_AFTER = 24 * 3600  # a source failing this long is mentioned in the next alert


def log(msg, logfile=None):
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}\n"
    if logfile:
        try:
            with open(logfile, "a") as fh:
                fh.write(line)
        except OSError:
            pass
    sys.stderr.write(line)


def fetch(url, user_agent):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def visible_text(markup):
    """Strip markup down to the text a reader would see."""
    markup = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", markup)
    return html.unescape(re.sub(r"(?s)<[^>]+>", "\n", markup))


def extract(source, markup):
    """Reduce a page to comparable lines.

    mode "links" keeps hyperlinks matching link_pattern, which is the reliable
    way to notice a new file appearing (a PDF whose name carries the month, for
    instance). mode "text" keeps visible text lines.

    Either mode can narrow further with keep_pattern / drop_pattern, which is
    how page furniture (nav, counters, cookie banners) is kept out of the diff.
    """
    keep = source.get("keep_pattern")
    drop = source.get("drop_pattern")

    if source.get("mode") == "links":
        pattern = source.get("link_pattern", r"[^\"]+")
        found = set()
        for m in re.finditer(r'href="(%s)"[^>]*>(.*?)</a>' % pattern, markup, re.I | re.S):
            label = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip()
            found.add(f"{label} -> {m.group(1)}" if label else m.group(1))
        lines = sorted(found)
    else:
        lines = []
        for raw in visible_text(markup).split("\n"):
            line = re.sub(r"\s+", " ", raw).strip()
            if line:
                lines.append(line)

    if keep:
        lines = [ln for ln in lines if re.search(keep, ln, re.I)]
    if drop:
        lines = [ln for ln in lines if not re.search(drop, ln, re.I)]

    # Drop near-empty lines and de-duplicate while keeping order.
    seen, out = set(), []
    for ln in lines:
        if re.fullmatch(r"[\W\d]{0,4}", ln) or ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    return out


def send_mail(cfg, subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("from_display", cfg["sender"])
    msg["To"] = ", ".join(cfg["recipients"])
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["sender"].split("@")[-1])
    msg.set_content(body)

    ctx = ssl.create_default_context()
    with smtplib.SMTP(cfg["smtp_host"], cfg.get("smtp_port", 587), timeout=TIMEOUT) as smtp:
        smtp.starttls(context=ctx)
        smtp.login(cfg["smtp_user"], cfg["smtp_password"])
        smtp.send_message(msg)


def describe(name, added, removed, max_added=60, max_removed=25):
    out = [f"== {name.upper()}"]
    out += [f"  + {ln}" for ln in added[:max_added]]
    if len(added) > max_added:
        out.append(f"  + ... and {len(added) - max_added} more new lines")
    out += [f"  - {ln}" for ln in removed[:max_removed]]
    if len(removed) > max_removed:
        out.append(f"  - ... and {len(removed) - max_removed} more removed lines")
    return out


def run(cfg, state_path, dry_run=False):
    logfile = cfg.get("logfile")
    ua = cfg.get("user_agent", DEFAULT_UA)
    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}

    now = time.time()
    sources = state.setdefault("sources", {})
    first_run = not sources
    changes, highlights, stale = [], [], []
    highlight = cfg.get("highlight_pattern")

    for source in cfg["sources"]:
        name, url = source["name"], source["url"]
        prev = sources.setdefault(name, {"lines": [], "ok": 0, "fail": 0})
        try:
            lines = extract(source, fetch(url, ua))
            if not lines:
                raise ValueError("no usable content (check keep_pattern)")
        except (urllib.error.URLError, ValueError, OSError, TimeoutError) as exc:
            prev["fail"] = prev.get("fail", 0) + 1
            log(f"{name}: fetch failed ({exc}); consecutive={prev['fail']}", logfile)
            if prev.get("ok") and now - prev["ok"] > STALE_AFTER:
                stale.append(f"{name} has not been readable since "
                             f"{datetime.fromtimestamp(prev['ok']).strftime('%b %d %H:%M')}")
            continue

        old = set(prev.get("lines", []))
        added = [ln for ln in lines if ln not in old]
        removed = [ln for ln in prev.get("lines", []) if ln not in set(lines)]
        prev.update({"lines": lines, "ok": now, "fail": 0})

        if (added or removed) and not first_run:
            changes.extend(describe(name, added, removed))
            if highlight:
                highlights.extend(ln for ln in added if re.search(highlight, ln, re.I))
            log(f"{name}: {len(added)} added, {len(removed)} removed", logfile)
        elif first_run:
            log(f"{name}: first run, storing {len(lines)} lines", logfile)

    if not dry_run:
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        with open(state_path, "w") as fh:
            json.dump(state, fh)

    if first_run:
        log("first run complete, baseline stored; no mail sent", logfile)
        return 0
    if not changes:
        return 0

    subject = cfg.get("subject", "Watched pages changed")
    if highlights and cfg.get("highlight_subject"):
        subject = cfg["highlight_subject"]

    body = [f"{cfg.get('intro', 'A watched page changed')} "
            f"({datetime.now().strftime('%b %d, %I:%M %p')}).", ""]
    if highlights:
        body += ["*** MATCHED WHAT YOU ARE WAITING FOR ***"]
        body += [f"  {ln}" for ln in highlights[:20]] + [""]
    body += changes
    if cfg.get("footer_links"):
        body += [""] + [f"{k}: {v}" for k, v in cfg["footer_links"].items()]
    if stale:
        body += [""] + [f"Note: {s}" for s in stale]
    if cfg.get("footer"):
        body += ["", cfg["footer"]]
    text = "\n".join(body)

    if dry_run:
        print(f"--- subject: {subject}\n{text}")
        return 0

    try:
        send_mail(cfg, subject, text)
        log(f"emailed {', '.join(cfg['recipients'])}: {subject}", logfile)
    except Exception as exc:
        # State is already saved: an unreachable mail server should not make the
        # next run replay the same diff forever.
        log(f"send failed: {exc}", logfile)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=os.environ.get("WATCHER_CONFIG", DEFAULT_CONFIG))
    ap.add_argument("--state", default=os.environ.get("WATCHER_STATE", DEFAULT_STATE))
    ap.add_argument("--dry-run", action="store_true",
                    help="print the email instead of sending it, and leave state untouched")
    ap.add_argument("--show", action="store_true",
                    help="print what each source currently extracts, for tuning patterns")
    args = ap.parse_args()

    with open(args.config) as fh:
        cfg = json.load(fh)

    if args.show:
        ua = cfg.get("user_agent", DEFAULT_UA)
        for source in cfg["sources"]:
            lines = extract(source, fetch(source["url"], ua))
            print(f"== {source['name']}: {len(lines)} lines")
            for ln in lines[:20]:
                print(f"   {ln}")
        return 0

    return run(cfg, args.state, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
