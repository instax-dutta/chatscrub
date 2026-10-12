import json
import re
import os
import subprocess
import sys
import tempfile

# Long enough to clear the default length band (40-350) after scrubbing.
FIXTURE = [
    # 1. mention + emote normalization
    {"id": "1000000000000000001", "author": "alice#0 (1)",
     "content": "yo bhai kaha se ho <@123> <:emote_name:456> sab badhiya",
     "timestamp": "2024-01-01T00:00:00Z", "replyTo": None, "attachments": 0},
    # 2. parent message provides reply context (gets blanked on PII)
    {"id": "1000000000000000002", "author": "bob#0 (2)",
     "content": "mere ko bhi bata do kya baat ho rahi hai yahan par abhi",
     "timestamp": "2024-01-01T00:01:00Z",
     "replyTo": "1000000000000000001", "attachments": 0},
    # 3. email must be redacted
    {"id": "1000000000000000003", "author": "carol#0 (3)",
     "content": "acha to mail karo mujhe isi address par aur baat karte hai baad me",
     "timestamp": "2024-01-01T00:02:00Z", "replyTo": None, "attachments": 0},
    # 4. phone number must drop the row
    {"id": "1000000000000000004", "author": "dave#0 (4)",
     "content": "call karo 9876543210 pe ek baar aur baat karte hai phir baad me",
     "timestamp": "2024-01-01T00:03:00Z", "replyTo": None, "attachments": 0},
    # 5. spoiler unwrap
    {"id": "1000000000000000005", "author": "eve#0 (5)",
     "content": "||spoilered text hai yahan|| aap log kya kar rahe ho abhi",
     "timestamp": "2024-01-01T00:04:00Z", "replyTo": None, "attachments": 0},
    # 6. url must be redacted
    {"id": "1000000000000000006", "author": "frank#0 (6)",
     "content": "join karo https://example.com/page is par aur dekho kaisa lagta hai",
     "timestamp": "2024-01-01T00:05:00Z", "replyTo": None, "attachments": 0},
    # 7. self-disclosed age must drop
    {"id": "1000000000000000007", "author": "grace#0 (7)",
     "content": "maine 15 saal pehle se yahan sab kuch dekha hai bahut kuch",
     "timestamp": "2024-01-01T00:06:00Z", "replyTo": None, "attachments": 0},
    # 8. too long must drop
    {"id": "1000000000000000008", "author": "heidi#0 (8)",
     "content": "bahut lamba message " * 20, "timestamp": "2024-01-01T00:07:00Z",
     "replyTo": None, "attachments": 0},
    # 9. role ping + snowflake
    {"id": "1000000000000000009", "author": "ivan#0 (9)",
     "content": "kuch nahi yaar bas aise hi <@&999888777666555444> timepass",
     "timestamp": "2024-01-01T00:08:00Z", "replyTo": None, "attachments": 0},
    # 10. mention-only spam must drop
    {"id": "1000000000000000010", "author": "judy#0 (10)",
     "content": "<@123> <@456> <@789> <@101> hehe", "timestamp": "2024-01-01T00:09:00Z",
     "replyTo": None, "attachments": 0},
    # 11. display-name ping must be redacted
    {"id": "1000000000000000011", "author": "ken#0 (11)",
     "content": "@someperson bhai tu bhi yahin tha kya kal raat ko wahan",
     "timestamp": "2024-01-01T00:10:00Z", "replyTo": None, "attachments": 0},
    # 12. config server word must be redacted
    {"id": "1000000000000000012", "author": "lara#0 (12)",
     "content": "kal fakeserver wale log aaye the phir se wahi purani kahani",
     "timestamp": "2024-01-01T00:11:00Z", "replyTo": None, "attachments": 0},
    # 13. config creator first name must be redacted
    {"id": "1000000000000000013", "author": "mike#0 (13)",
     "content": "examplename ne bola tha ki woh aaj zaroor aa jayenge yahan",
     "timestamp": "2024-01-01T00:12:00Z", "replyTo": None, "attachments": 0},
    # 14. emote name carrying a config identity must be stripped
    {"id": "1000000000000000014", "author": "nina#0 (14)",
     "content": "aaj bahut maza aaya sab mil ke <:fakeserverLaugh:999> karte hai phir",
     "timestamp": "2024-01-01T00:13:00Z", "replyTo": None, "attachments": 0},
]

# Fixture PII is synthetic. alice@example.com is a reserved domain.
_ = "alice@example.com"


def run_pipeline() -> int:
    with tempfile.TemporaryDirectory() as td:
        inp = os.path.join(td, "sample-chat.json")
        with open(inp, "w") as fh:
            json.dump(FIXTURE, fh)
        cfg_text = """
[paths]
input_globs = ['%s/*.json']
out = 'out.jsonl'
manifest = '.manifest'

[filters]
min_len = 40
max_len = 320

[identity]
server_names = ['fakeserver']
creator_names = ['\\bexamplename\\b']
# all four knobs get exercised
emote_interior = ['fakeserver']
emote_prefixes = ['emote_']
""" % td
        cfg = os.path.join(td, "config.toml")
        with open(cfg, "w") as fh:
            fh.write(cfg_text)
        env = {**os.environ, "PYTHONPATH": "/tmp/chat-scrub/src"}
        r = subprocess.run(
            [sys.executable, "-m", "chatscrub", "--config", cfg],
            capture_output=True, text=True, cwd="/tmp/chat-scrub/src", env=env,
        )
        print("--- run log ---")
        print(r.stderr.strip())
        assert r.returncode == 0, r.stderr

        out_path = os.path.join(td, "out.jsonl")
        rows = [json.loads(l) for l in open(out_path)]
        print(f"--- {len(rows)} rows kept of {len(FIXTURE)} ---")
        for row in rows:
            print(f"  {row['text'][:100]}")
            if row["reply_to_text"]:
                print(f"    ctx -> {row['reply_to_text'][:60]}")

        blob = json.dumps(
            [{k: r[k] for k in ("text", "reply_to_text", "topics")}
             for r in rows],
            ensure_ascii=False,
        )
        # PII must be gone (message snowflakes are kept in `id` by design)
        forbidden = ["9876543210", "alice@example.com", "<@", "fakeserver",
                     "example.com", "<:", "999888777666555444",
                     "@someperson", "examplename"]
        for needle in forbidden:
            assert needle not in blob, f"LEAK: {needle!r} survived scrubbing"
        # expected redaction markers present
        for marker in ["[MENTION]", "[EMOTE_", "[LINK]", "[SERVER]"]:
            assert marker in blob, f"MISSING: {marker}"
        # real names redacted
        assert "examplename" not in blob
        # identity must never nest inside an emote token
        assert not re.search(r"\[EMOTE_[^]]*(\[SERVER\]|\[NAME\])", blob), \
            "identity nested inside emote token"
        print("\nall assertions passed")

        r2 = subprocess.run(
            [sys.executable, "-m", "chatscrub", "--config", cfg],
            capture_output=True, text=True, cwd="/tmp/chat-scrub/src", env=env,
        )
        rows2 = [json.loads(l) for l in open(out_path)]
        assert len(rows2) == len(rows), "re-run duplicated rows"
        print(f"idempotency OK ({len(rows2)} rows after re-run)")
    return 0


if __name__ == "__main__":
    sys.exit(run_pipeline())
