"""Core scrub pipeline for chat exports.

Turns a raw chat export (JSON list of message objects) into an anonymized,
safety-filtered corpus. All identity patterns that are specific to a source
(server names, creator names, emote prefixes) live in the config file, never
in code.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable

# ---------------------------------------------------------------- structural


EMOJI_EMOTE_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
EMOTE_TOKEN_RE = re.compile(r"\[EMOTE_([^\]]*)\]")
MENTION_RE = re.compile(r"<[@#]&?!?\d+>")
SNOWFLAKE_RE = re.compile(r"\b\d{17,20}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HANDLE_RE = re.compile(r"@\w{2,32}")
URL_RE = re.compile(r"https?://\S*|www\.\S+")
PHONE_RE = re.compile(r"\b[6-9]\d{9}\b")  # 10-digit mobile, leading 6-9
SPOILER_RE = re.compile(r"\|\|(.+?)\|\|", re.S)

FULLNAME_RE = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+\b")
FULLNAME_ALLOW = {
    "good morning", "good night", "good evening", "good afternoon",
    "happy birthday", "merry christmas", "happy new year", "new year",
    "april fool", "april fools", "real quick", "mother nature",
}
INTRO_RE = re.compile(
    r"\b(my name is|this is|i am|i'm|call me|this side) ([A-Z][a-z]{2,})\b", re.I
)

TAG_RE = re.compile(
    r"\[(MENTION|ID|LINK|EMOTE_[^\]]*|SERVER|NAME|REDACTED_EMAIL)\]"
)


def anon_id(raw: str) -> str:
    """Stable anonymous handle for a raw identifier."""
    return "USER_" + hashlib.sha256(raw.encode()).hexdigest()[:8]


def real_text_len(text: str) -> int:
    """Length excluding replacement tags - kills mention/emote-only spam."""
    return len(TAG_RE.sub("", text).strip())


@dataclass
class ScrubConfig:
    """Identity patterns specific to one source.

    server_names  : regexes for the room / brand -> [SERVER]
    creator_names : regexes for real people's names -> [NAME]
    emote_prefixes: emote-name prefixes belonging to that room (e.g. an
                    initials-prefixed emote pack) -> stripped
    emote_interior: regexes removed from inside emote names
    extra_blocklist: additional per-source row-drop patterns
    """

    server_names: list[str] = field(default_factory=list)
    creator_names: list[str] = field(default_factory=list)
    emote_prefixes: list[str] = field(default_factory=list)
    emote_interior: list[str] = field(default_factory=list)
    extra_blocklist: list[str] = field(default_factory=list)

    def compile(self) -> "CompiledScrub":
        return CompiledScrub(self)

    def emote_prefixes_regex(self) -> str:
        """Alternation of escaped prefixes, for anchored matching."""
        return "|".join(re.escape(p) for p in self.emote_prefixes if p)


def _or(patterns: Iterable[str], flags: int = re.I) -> re.Pattern | None:
    parts = [p for p in patterns if p]
    return re.compile("|".join(f"(?:{p})" for p in parts), flags) if parts else None


@dataclass
class CompiledScrub:
    """Pre-compiled config. Reuse across many files."""

    cfg: ScrubConfig
    server_re: re.Pattern | None = None
    creator_re: re.Pattern | None = None
    emote_interior_re: re.Pattern | None = None
    emote_prefix_re: re.Pattern | None = None
    extra_block_re: re.Pattern | None = None

    def __post_init__(self) -> None:
        self.server_re = _or(self.cfg.server_names)
        self.creator_re = _or(self.cfg.creator_names)
        self.emote_interior_re = _or(self.cfg.emote_interior)
        # prefixes match the inner name only, e.g. 'gf' strips 'gf_' prefixes
        self.emote_prefix_re = _or(self.cfg.emote_prefixes) if self.cfg.emote_prefixes else None
        self.extra_block_re = _or(self.cfg.extra_blocklist)

    # ---------------------------------------------------------------- text

    @staticmethod
    def _scrub_fullnames(text: str) -> str:
        def _sub(m):
            return m.group(0) if m.group(0).lower() in FULLNAME_ALLOW else "[NAME]"

        return FULLNAME_RE.sub(_sub, text)

    def _emote_token(self, inner: str) -> str:
        """Normalize one emote's inner name, stripping room-specific identity."""
        if self.emote_interior_re:
            inner = self.emote_interior_re.sub("", inner).strip("_")
        if self.emote_prefix_re:
            # drop any leading configured prefix (keeps the rest of the name)
            m = re.match(rf"((?:{self.cfg.emote_prefixes_regex()}))[_]?(.*)$", inner)
            if m:
                inner = (m.group(2) or "").strip("_")
        return f"[EMOTE_{inner}]" if inner else "[EMOTE]"

    def _normalize_emotes(self, text: str) -> str:
        """Convert Discord emotes to tokens, then sanitize their inner names.
        Runs BEFORE prose identity scrubs so identities can never nest
        inside a token (e.g. [EMOTE_[SERVER]Laugh])."""
        text = EMOJI_EMOTE_RE.sub(r"[EMOTE_\1]", text)
        return EMOTE_TOKEN_RE.sub(lambda m: self._emote_token(m.group(1)), text)

    def _prose_scrub(self, text: str) -> str:
        """Identity scrub on prose only - emote tokens are split out so an
        identity can never nest inside one (e.g. [EMOTE_[SERVER]Laugh])."""
        segments = re.split(r"(\[EMOTE_[^\]]*\])", text)
        for i, seg in enumerate(segments):
            if seg.startswith("[EMOTE_"):
                continue
            if self.server_re:
                seg = self.server_re.sub("[SERVER]", seg)
            if self.creator_re:
                seg = self.creator_re.sub("[NAME]", seg)
            segments[i] = seg
        return "".join(segments)

    def clean(self, text: str) -> str:
        """Full anonymizing pass. Idempotent."""
        if not text:
            return ""
        text = unicodedata.normalize("NFKC", text)
        text = SPOILER_RE.sub(r"\1", text)
        text = self._normalize_emotes(text)
        text = self._prose_scrub(text)
        text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
        text = URL_RE.sub("[LINK]", text)
        text = MENTION_RE.sub("[MENTION]", text)
        text = SNOWFLAKE_RE.sub("[ID]", text)
        text = HANDLE_RE.sub("[MENTION]", text)
        text = INTRO_RE.sub(r"\1 [NAME]", text)
        text = self._scrub_fullnames(text)
        return re.sub(r"[ \t]+", " ", text).strip()

    def ctx_unsafe(self, text: str) -> bool:
        """Parent-message contexts bypass filters - blank them on hard hits."""
        return bool(
            PHONE_RE.search(text) or EMAIL_RE.search(text)
            or SNOWFLAKE_RE.search(text)
        )


# ---------------------------------------------------------------- safety gates

# Default blocklists. Extend via config.extra_blocklist.
ABUSE = re.compile(
    r"\b(mc|bc|bhosdike|bhadw|chutiya|gandu|madarchod|randi|bsdk)\b", re.I
)
GENDER_PROBE = re.compile(
    r"ladk[ai]|ladk[aeiou]|single|crush|boyfriend|girlfriend|babu|shona|pyaar|"
    r"shaadi|middle|gender|ladki ho|ladka ho",
    re.I,
)
AGE_SCHOOL = re.compile(
    r"\bclass\s?\d\b|\b(10th|12th)\b|\b(school|college|exam|padhai|umar|"
    r"tuition|teacher)\b|saal ka|years old|sir mam",
    re.I,
)
SELF_AGE = re.compile(
    r"(?i)\b(i am|i'm|\bim\b|age|aged)\s*\d{1,2}\b|"
    r"\b\d{1,2}\s*(years old|year old|saal ka|saal ki)\b"
)
PROMO_LURE = re.compile(
    r"dm me|dm kar|dm karo|dm kro|contact me|inbox me|join my server|subscribe|"
    r"views nhi|nitro|free minecraft|free acc|giveaway|link is above|profile mei",
    re.I,
)
RELIGIOUS = re.compile(
    r"jai shree ram|jai mata|allah|inshallah|har har|waheguru", re.I
)
CONTACT_DOX = re.compile(
    r"\b\d{6}\b|address|phone|number de|call me|instagram.*[_.][a-z]+", re.I
)
STAFF_NAMING = re.compile(r"(?i)\b[a-z]+\s+(sir|mam|ma'am)\b")
DRUGS = re.compile(
    r"\b(maal|ganja|weed|charas|darru|daaru|sutta|cigarette|hookah|nashe?di)\b",
    re.I,
)

ATTACK_HOWTO = re.compile(
    r"(?i)(ignore all previous|disregard.*instructions|you are now DAN|"
    r"do anything now|jailbreak.*prompt|bypass.*(filter|guardrail)|"
    r"unsensored prompt|break all of .* training)"
)


def strip_emotes_for_filter(text: str) -> str:
    """Emote names must not drive the drug/safety match."""
    return EMOJI_EMOTE_RE.sub(" ", text)


# ---------------------------------------------------------------- topics

DEFAULT_TOPICS: list[tuple[str, str]] = [
    ("greeting_intro", r"\b(hi+|hello|hlo|hey|yo|namaste|kaha se|kaise ho|kkrh|"
                        r"kya kar r|welcome|good (morning|night)|so jao)\b"),
    ("gaming", r"minecraft|smp|hypixel|bedwars|bgmi|valo(rant)?|gta|roblox|"
                r"server|member banna|\bip\b"),
    ("food_sleep", r"khana|kha liya|bhook|sick|fever|so jao|nind|chai|maggi|"
                   r"good night|good morning"),
    ("media", r"naruto|one piece|demon slayer|\bjjk\b|solo leveling|anime|song|"
              r"music|movie|web series|stream"),
    ("server_meta", r"mod|admin|timeout|\bban\b|mute|members|server|emote|"
                    r"nitro|streak"),
    ("other_chitchat", r"."),
]


def default_topic_rules() -> list[tuple[str, re.Pattern]]:
    return [(name, re.compile(rx, re.I)) for name, rx in DEFAULT_TOPICS]


# ---------------------------------------------------------------- processing

def normalize_messages(data) -> list[dict]:
    """Accept a flat list, or a part-wrapped {'messages': [...]} export."""
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        out = []
        part = data.get("part", "p")
        ch = data.get("channelId", "part")
        for i, sub in enumerate(data["messages"]):
            row = dict(sub)
            row.setdefault("id", f"{ch}-{part}-{i}")
            out.append(row)
        return out
    return data or []


def member_name_regex(messages: list[dict]) -> re.Pattern | None:
    """Small circles: derive a first-name regex from members' handles."""
    stop = {"bot", "exe", "io", "dmg", "jr", "dev", "official", "plays", "lord",
            "prime", "swan", "the", "mr", "general", "user", "deleted"}
    names: set[str] = set()
    for m in messages:
        handle = (m.get("author", "") or "").split("#")[0].lower()
        for tok in re.split(r"[^a-z]+", handle):
            if len(tok) >= 4 and tok not in stop:
                names.add(tok)
        for pre in ("the", "iam", "its", "im", "itz", "mr", "og"):
            if handle.startswith(pre) and len(handle) - len(pre) >= 4:
                names.add(handle[len(pre):])
    if not names:
        return None
    return re.compile(
        r"\b(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\b",
        re.I,
    )


def process_file(
    path: str,
    source_label: str,
    scrub: CompiledScrub,
    seen_ids: set[str],
    state: dict | None = None,
    *,
    min_len: int = 60,
    max_len: int = 350,
    max_dupes: int = 3,
    author_cap: int = 150,
    small_circle_authors: int = 150,
    real_text_floor: int = 25,
    topic_rules: list[tuple[str, re.Pattern]] | None = None,
    drop_fn: Callable[[str], str | None] | None = None,
) -> tuple[list[dict], Counter]:
    """Scrub one export file into rows. Returns (rows, drop_reasons).

    state carries cross-file 'seen_text' so the dup cap holds globally.
    Per-author caps stay per-file on purpose: regulars ARE the room's voice.
    """
    import json

    with open(path, encoding="utf-8") as fh:
        messages = normalize_messages(json.load(fh))

    by_id = {m["id"]: m for m in messages}
    n_authors = len({m.get("author", "?") for m in messages})
    member_re = (
        member_name_regex(messages)
        if n_authors < small_circle_authors else None
    )
    topics = topic_rules or default_topic_rules()

    seen_text = state["seen_text"] if state else Counter()
    per_author: Counter = Counter()
    kept: list[dict] = []
    dropped: Counter = Counter()

    for m in messages:
        if m.get("id") in seen_ids:
            dropped["seen_id"] += 1
            continue
        raw = (m.get("content") or "").strip()
        if not raw:
            dropped["empty"] += 1
            continue
        if not (min_len <= len(raw) <= max_len):
            dropped["length"] += 1
            continue
        low = raw.lower()
        if ABUSE.search(low):
            dropped["abuse"] += 1
            continue
        if GENDER_PROBE.search(low):
            dropped["gender_probe"] += 1
            continue
        if AGE_SCHOOL.search(low):
            dropped["age_school"] += 1
            continue
        if SELF_AGE.search(raw):
            dropped["self_age"] += 1
            continue
        if PROMO_LURE.search(low):
            dropped["promo_lure"] += 1
            continue
        if RELIGIOUS.search(low):
            dropped["religious"] += 1
            continue
        if CONTACT_DOX.search(low):
            dropped["contact_dox"] += 1
            continue
        if PHONE_RE.search(raw):
            dropped["phone"] += 1
            continue
        if ATTACK_HOWTO.search(raw):
            dropped["attack_howto"] += 1
            continue
        if STAFF_NAMING.search(raw):
            dropped["staff_naming"] += 1
            continue
        if DRUGS.search(strip_emotes_for_filter(raw)):
            dropped["drugs"] += 1
            continue
        if drop_fn and (reason := drop_fn(raw)):
            dropped[reason] += 1
            continue
        if scrub.extra_block_re and scrub.extra_block_re.search(raw):
            dropped["config_blocklist"] += 1
            continue

        text = scrub.clean(raw)
        if len(text) < 40:
            dropped["postclean_short"] += 1
            continue
        if member_re:
            text = member_re.sub("[NAME]", text)
        if real_text_len(text) < real_text_floor:
            dropped["tag_spam"] += 1
            continue
        if seen_text[text] >= max_dupes:
            dropped["duplicate"] += 1
            continue

        author = anon_id(m.get("author", "?"))
        if per_author[author] >= author_cap:
            dropped["author_cap"] += 1
            continue
        seen_text[text] += 1
        per_author[author] += 1
        seen_ids.add(m["id"])

        ctx = ""
        parent_id = m.get("replyTo")
        if parent_id and parent_id in by_id:
            ctx = scrub.clean((by_id[parent_id].get("content") or "")[:300])
            if member_re:
                ctx = member_re.sub("[NAME]", ctx)
            if scrub.ctx_unsafe(ctx):
                ctx = ""
                dropped["ctx_blanked"] += 1

        tags = [name for name, rx in topics if rx.search(raw)] or ["other_chitchat"]
        kept.append({
            "id": m["id"],
            "author": author,
            "timestamp": m.get("timestamp"),
            "text": text,
            "reply_to_text": ctx,
            "topics": tags,
            "has_emote": "[EMOTE_" in text,
            "char_len": len(text),
            "source": source_label,
        })

    return kept, dropped


# ---------------------------------------------------------------- convenience

_DEFAULT_COMPILED = ScrubConfig().compile()


def clean_text(text: str, scrub: "CompiledScrub | None" = None) -> str:
    """Scrub one string with the default (empty-identity) config."""
    return (scrub or _DEFAULT_COMPILED).clean(text)
