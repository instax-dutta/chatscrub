# chatscrub

Turn a raw chat export into a privacy-scrubbed, safety-filtered corpus.

Opinionated defaults, config-driven identity rules, incremental and idempotent
by design. Built for large single-channel exports (hundreds of thousands to
millions of messages) where the interesting content is the tail.

## Why

Scraping a chat export is easy. Publishing one safely is not. The failure modes
are boring and they all bite at scale:

| Failure | Scale symptom |
|---|---|
| Identity leakage | handles, real names, self-intros, branded emote sets |
| Contactable PII | phone numbers, emails, social handles in *reply* context |
| Structural PII | snowflake IDs, invite links, CDN URLs with signed expiry tokens |
| Minors | age self-disclosure, class/school references |
| Bot spam | one bot is 40% of the corpus |
| Duplicates | a repeating templated bot message drowns real signal |

`chatscrub` handles all of these in one pass, with every identity rule kept in
config so your scrub policy is reviewable without reading code.

## Install

Requires Python 3.11+ (uses stdlib `tomllib`).

```bash
git clone https://github.com/<you>/chatscrub && cd chatscrub
pip install -e .
chatscrub --config config.toml
```

Or without installing:

```bash
PYTHONPATH=src python3 -m chatscrub --config config.toml
```

## Quickstart

```bash
cp config.example.toml config.toml
# edit config.toml: input_globs, and [identity] for your source
chatscrub --config config.toml
```

Output is JSONL, one row per retained message:

```json
{
  "id": "1000000000000000001",
  "author": "USER_63fe4b86",
  "timestamp": "2024-01-01T00:00:00Z",
  "text": "yo bhai kaha se ho [MENTION] [EMOTE_Laugh] sab badhiya",
  "reply_to_text": "parent message context or empty",
  "topics": ["greeting_intro"],
  "has_emote": true,
  "char_len": 53,
  "source": "room-a1b2c3"
}
```

## What it does

### Identity scrubbing (the part you must configure)

Everything source-specific lives in `[identity]`. Four knobs:

| Knob | Matches | Replaced with |
|---|---|---|
| `server_names` | room / brand names in prose, incl. spaced variants | `[SERVER]` |
| `creator_names` | real people's first names | `[NAME]` |
| `emote_prefixes` | branded emote-name prefixes (`gf_`, `ab_`) | stripped |
| `emote_interior` | identities embedded *inside* emote names | stripped |

```toml
[identity]
server_names = ['\bsample-server\b', 'sampleserver']
creator_names = ['\bexample-name\b']
emote_prefixes = ['gf']
emote_interior = ['sample']
```

Prose scrubs never touch emote tokens. `<:sampleLaugh:123>` becomes
`[EMOTE_Laugh]`, never `[EMOTE_[SERVER]Laugh]`.

### Structural PII (always on)

| Input | Output |
|---|---|
| `<@123>`, `<@&role>`, `#chan`, `@handle` | `[MENTION]` |
| 17-20 digit IDs | `[ID]` |
| `user@example.com` | `[REDACTED_EMAIL]` |
| any URL, incl. signed CDN links and invite links | `[LINK]` |
| `<:name:id>` / `<a:name:id>` | `[EMOTE_name]` |
| 10-digit mobile numbers | row dropped |
| `My name is X` / `this side X` / `call me X` | `[NAME]` |
| `Firstname Lastname` (with a greetings allowlist) | `[NAME]` |
| `\|\|spoiler\|\|` | unwrapped |

Authors are hashed to `USER_<sha8>` - stable across runs for dedup, useless to
anyone else.

### Reply-context guard

Reply targets bypass every filter, so a parent message can leak a phone number
or email into otherwise-clean rows. Any context matching a hard PII pattern is
dropped, and counted as `ctx_blanked` in the run report.

### Safety gates (rows dropped)

Abuse slang, gender/relationship probing, age/school references, self-disclosed
ages, promo and DM lures, religious slogans, contact dox, phone numbers,
jailbreak/attack how-tos, teacher-naming (`X sir`/`X mam`), drug slang.

Extend with `[filters].extra_blocklist`.

### Signal filters

| Filter | Default | Why |
|---|---|---|
| Length band | 60-350 chars | greetings and wall-of-text both go |
| Real-text floor | 25 non-tag chars | kills `<@a> <@b> <@c>` spam |
| Text duplicate cap | 3 copies, global | bot templates stop dominating |
| Per-author cap | 150 rows per file | stops a chatty bot |
| Small-circle names | < 150 authors | handles become a first-name regex |

Length band and caps are all in `[filters]`/`[caps]`.

### Incremental and idempotent

A manifest records processed filenames; dedup is global by message ID, not
per-file. Point it at a folder that keeps growing and it picks up only what is
new:

```bash
chatscrub --config config.toml      # first run
chatscrub --config config.toml      # no-op: nothing new
# drop new-export.json into the folder
chatscrub --config config.toml      # processes only the new file
```

Re-running cannot duplicate rows. Cross-file duplicate caps persist through
the existing output file.

## Output schema

| Field | Type | Meaning |
|---|---|---|
| `id` | string | original message ID (kept for dedup and traceability) |
| `author` | string | `USER_<sha8>` anonymous handle |
| `timestamp` | string | ISO-8601 when present |
| `text` | string | scrubbed content |
| `reply_to_text` | string | scrubbed parent, or empty |
| `topics` | list[string] | weak regex topic tags |
| `has_emote` | bool | contains an `[EMOTE_*]` token |
| `char_len` | int | length of `text` |
| `source` | string | neutral room label |

Topic tags are **weak labels** - regex heuristics on the raw text. Fine for
stratified sampling and exploratory splits; do not ship them as gold annotations.
Override with your own rules by passing `topic_rules` to `process_file`.

## Library use

```python
from chatscrub import ScrubConfig, CompiledScrub, clean_text

scrub = ScrubConfig(server_names=['\bfake-server\b']).compile()
clean_text("<@1> yo fake-server bhai <:gf_Hi:2>", scrub)
# "[MENTION] yo [SERVER] bhai [EMOTE_Hi]"
```

## Tests

```bash
PYTHONPATH=src python3 tests/test_smoke.py
```

Covers each redaction type, the safety gates, the emote-nesting guard,
idempotency, and a "no leak" assertion over the output.

## Known limits

- Topic tagging is regex-based and tuned for code-mixed casual chat. Edit
  `DEFAULT_TOPICS` in `scrub.py` for other domains or languages.
- Per-author caps are per-file on purpose: regulars *are* a room's voice.
  Raising `author_cap` too high lets one user dominate; lowering it too far
  distorts the room.
- The built-in safety lists are tuned for casual South-Asian chat. Audit
  drops against your own source before trusting the numbers.
- Full-name detection is heuristic and will miss non-Latin scripts.
- It does not identify consent. Anonymizing rows is not the same as having
  permission to publish the corpus.

## License

MIT
