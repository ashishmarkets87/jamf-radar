#!/usr/bin/env python3
"""
Jamf Radar daily updater.

Runs unattended in GitHub Actions. Reads the current jamf_data.json, asks Claude
(with the built-in web_search tool) to find genuinely NEW Jamf news items and
Mac Fix-It KB entries not already present, merges them in, re-sorts/caps the
arrays, stamps a fresh lastUpdated, and rewrites both jamf_data.json and the
embedded <script id="jamf-data"> block in index.html so the two stay in sync.

Only asking Claude for the *delta* (new items) rather than the full merged
history keeps token usage/cost low and avoids truncation issues as the
archives grow toward their caps (60 news / 40 kb).
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import anthropic

MODEL = "claude-sonnet-5"
NEWS_CAP = 60
KB_CAP = 40
DATA_PATH = "jamf_data.json"
HTML_PATH = "index.html"

NEWS_CATEGORIES = [
    "Release", "Security", "Events", "Deep Dive", "Troubleshooting",
    "Tips & Tricks", "Community", "Career", "AI Governance",
]
KB_CATEGORIES_HINT = (
    "Hardware & Boot, Connectivity, Updates, Storage, Security, Performance, "
    "Peripherals, Backup, Battery, Enterprise / MDM, Display, Input Devices, "
    "Cloud & Sync, Audio"
)

NEWS_SEARCH_TOPICS = [
    "Jamf Pro release notes latest",
    "Jamf Pro known issues",
    "Jamf Connect troubleshooting",
    "Jamf community troubleshooting",
    "Jamf blog news",
    "Declarative Device Management Jamf tips",
    "Jamf Nation discussions",
    "JNUC news",
]
KB_SEARCH_TOPICS = [
    "Wi-Fi/Bluetooth connectivity", "kernel panics/reboots", "macOS update failures",
    "storage \"Other\" bloat", "FileVault/encryption problems", "slow performance/beach ball",
    "printer/peripheral issues", "Time Machine/backup failures", "battery drain",
    "MDM/enrollment problems", "external display issues", "keyboard/trackpad issues",
    "iCloud sync problems", "Spotlight/search issues", "audio issues",
    "camera/webcam issues", "USB-C/Thunderbolt dock issues",
]


def load_data():
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_prompt(existing):
    news_context = [
        {"id": n["id"], "title": n["title"], "url": n["url"], "date": n["date"]}
        for n in existing.get("news", [])
    ]
    kb_context = [
        {"id": k["id"], "title": k["title"], "tags": k.get("tags", [])}
        for k in existing.get("kb", [])
    ]

    return f"""You are the "Jamf Radar" research agent. Research (1) the latest Jamf-related \
news, releases, security updates, tips/tricks, troubleshooting threads and community \
discussions, and (2) common macOS/Mac hardware issues and their best solutions, for a \
two-tab dashboard used by an IT support engineer learning Jamf administration.

Use your web_search tool extensively. Today's date context should be inferred from your \
search results (search for current news, don't assume a date).

PART A — NEWS. Cover these trusted sources: support.jamf.com, jamf.com/blog, \
learn.jamf.com, community.jamf.com (Tech Thoughts, Release Announcements, General \
Discussions, known issues), Reddit r/Jamf, MacAdmins-adjacent blogs (e.g. \
macjediwizard.blog), and JNUC / Jamf Nation Live event news. Search topics (adjust \
wording, include current year/month): {", ".join(f'"{t}"' for t in NEWS_SEARCH_TOPICS)}.

Valid categories: {", ".join(NEWS_CATEGORIES)}.

Here are the news items ALREADY in the dataset (compare by URL/title — do NOT repeat \
these, only return genuinely NEW items not in this list):
{json.dumps(news_context, indent=2)}

PART B — MAC FIX-IT KB. Research common macOS/Mac hardware issues and best-practice \
solutions from support.apple.com, discussions.apple.com, Ask Different (Stack \
Exchange), MacRumors Forums, Macworld/9to5Mac/Setapp/MacPaw, and Jamf Community for \
MDM/enterprise-specific issues. Rotate through topics not yet well covered: \
{", ".join(KB_SEARCH_TOPICS)}. Category label ideas: {KB_CATEGORIES_HINT}.

Here are the KB entries ALREADY in the dataset (compare by title/tags — do NOT repeat \
these, only return genuinely NEW, distinct issues not in this list):
{json.dumps(kb_context, indent=2)}

OUTPUT FORMAT — respond with ONLY a single valid JSON object, no markdown fences, no \
commentary before or after, matching exactly this shape:

{{
  "news": [
    {{
      "category": "<one of: {", ".join(NEWS_CATEGORIES)}>",
      "title": "<concise headline>",
      "date": "YYYY-MM-DD",
      "source": "<publisher/site name>",
      "url": "<direct link>",
      "summary": "<2-4 sentence plain-English summary, practical and specific>",
      "tags": ["short","lowercase","keywords"]
    }}
  ],
  "kb": [
    {{
      "category": "<short category label>",
      "title": "<concise issue name>",
      "difficulty": "<Easy | Medium | Advanced>",
      "appliesTo": "<e.g. Both, Apple Silicon & Intel, MacBook (battery models), Managed (Jamf/MDM) devices>",
      "symptom": "<1-2 sentence plain description of what the user sees>",
      "causes": ["<likely cause 1>", "<likely cause 2>"],
      "solution": ["<step 1>", "<step 2>", "... ordered, actionable, specific>"],
      "source": "<publisher/site name>",
      "url": "<direct link>",
      "tags": ["short","lowercase","keywords"]
    }}
  ]
}}

If you find no genuinely new items for a section, return an empty array for that \
section — do not invent items or repeat existing ones. Keep summaries practical and \
geared toward someone doing IT support today who is building toward a Jamf admin / \
Jamf deployment & troubleshooting career. Aim for up to 6 new news items and up to 4 \
new KB entries per run, prioritizing quality and genuine novelty over quantity."""


def extract_text(response):
    parts = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts)


def parse_json_response(text):
    text = text.strip()
    # Strip markdown code fences if the model added them despite instructions.
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence_match:
        text = fence_match.group(1)
    else:
        # Fall back to the first {...} block in the text.
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1:
            text = text[first:last + 1]
    return json.loads(text)


def call_claude(prompt):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 30}],
        messages=[{"role": "user", "content": prompt}],
    )
    text = extract_text(response)
    return parse_json_response(text)


def next_id(items, prefix=""):
    nums = []
    for it in items:
        raw = str(it.get("id", ""))
        raw = raw[len(prefix):] if prefix and raw.startswith(prefix) else raw
        if raw.isdigit():
            nums.append(int(raw))
    return (max(nums) + 1) if nums else 1


def merge(existing, delta):
    news = list(existing.get("news", []))
    kb = list(existing.get("kb", []))

    existing_news_urls = {n.get("url") for n in news}
    existing_kb_titles = {k.get("title", "").strip().lower() for k in kb}

    next_news_id = next_id(news)
    for item in delta.get("news", []):
        if item.get("url") in existing_news_urls:
            continue
        item["id"] = str(next_news_id)
        next_news_id += 1
        news.append(item)
        existing_news_urls.add(item.get("url"))

    next_kb_num = next_id(kb, prefix="kb")
    for item in delta.get("kb", []):
        if item.get("title", "").strip().lower() in existing_kb_titles:
            continue
        item["id"] = f"kb{next_kb_num}"
        next_kb_num += 1
        kb.append(item)
        existing_kb_titles.add(item.get("title", "").strip().lower())

    news.sort(key=lambda n: n.get("date", ""), reverse=True)
    news = news[:NEWS_CAP]
    kb = kb[:KB_CAP]

    return {
        "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "news": news,
        "kb": kb,
    }


def write_data(data):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def write_html(data):
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    pattern = re.compile(
        r'(<script id="jamf-data" type="application/json">)(.*?)(</script>)',
        re.S,
    )
    replacement_json = json.dumps(data, indent=2)
    new_html, count = pattern.subn(
        lambda m: m.group(1) + "\n" + replacement_json + "\n" + m.group(3),
        html,
        count=1,
    )
    if count != 1:
        print("ERROR: could not find <script id=\"jamf-data\"> block in index.html", file=sys.stderr)
        sys.exit(1)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)


def main():
    existing = load_data()
    prompt = build_prompt(existing)

    try:
        delta = call_claude(prompt)
    except Exception as e:
        print(f"WARNING: Claude research call failed ({e}); only bumping lastUpdated.", file=sys.stderr)
        delta = {"news": [], "kb": []}

    merged = merge(existing, delta)
    write_data(merged)
    write_html(merged)

    print(f"Done. news={len(merged['news'])} kb={len(merged['kb'])} lastUpdated={merged['lastUpdated']}")


if __name__ == "__main__":
    main()
