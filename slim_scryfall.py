#!/usr/bin/env python3
"""
slim_scryfall.py -- collapse a Scryfall bulk dump into a compact deckbuilding index.

Usage:  python3 slim_scryfall.py <default-cards.jsonl.gz> <out.jsonl> [oracle-tags.jsonl.gz]

The optional third argument is Scryfall's "Oracle Tags" bulk file (from the same
Bulk Data page). If supplied, each card gains a "tags" array of community
function tags -- ramp, creature-removal, creates-token, and so on -- joined on
oracle_id. Tags are community-maintained and coverage is uneven, so treat them
as a filter to narrow candidates, not as ground truth; verify against oracle_text.

Input  may be .json or .jsonl, plain or .gz (Scryfall serves a JSON array;
       some mirrors serve JSONL -- both are handled).
Output is gzipped if the filename ends in .gz, otherwise plain text.

Keeps one record per unique card (by oracle_id), merging across all printings:
  - cheapest paper USD price seen
  - lowest rarity seen
  - earliest printing's set / set_type / released, kept consistent with each other
  - number of printings
Drops: non-English, digital-only, oversized, memorabilia, art series.
Keeps: normal cards, tokens, emblems, planes, schemes, and all DFC/split/adventure faces.
"""
import sys, json, gzip

DROP_LAYOUTS = {"art_series"}
# Oversized / gold-bordered novelty printings duplicate real cards and carry
# junk prices, which would poison the cheapest-printing aggregate.
DROP_SET_TYPES = {"memorabilia"}
RARITY_ORDER = {"common": 0, "uncommon": 1, "rare": 2, "special": 3, "mythic": 4, "bonus": 5}
# Formats worth tracking; everything else is noise for Commander/Horde.
KEEP_FORMATS = ("commander", "vintage", "legacy", "modern", "pauper", "duel", "predh")

FACE_FIELDS = ("name", "mana_cost", "type_line", "oracle_text", "power", "toughness",
               "loyalty", "defense", "colors")


def slim_face(f):
    return {k: f[k] for k in FACE_FIELDS if f.get(k) not in (None, "")}


def price_of(d):
    p = d.get("prices") or {}
    for k in ("usd", "usd_foil", "usd_etched"):
        if p.get(k):
            try:
                return float(p[k])
            except ValueError:
                pass
    return None


def open_text(path, mode="rt"):
    if path.endswith(".gz"):
        extra = {"compresslevel": 9} if "w" in mode else {}
        return gzip.open(path, mode, encoding="utf-8", **extra)
    return open(path, mode.replace("t", ""), encoding="utf-8")


def iter_records(src):
    """Yield card dicts from JSONL or from a single JSON array.

    JSONL is streamed line by line so the full 600MB never sits in memory.
    """
    with open_text(src) as fh:
        first = fh.read(1)
        while first and first.isspace():
            first = fh.read(1)
        if not first:
            return
        if first == "[":                       # JSON array: must load whole
            yield from json.loads(first + fh.read())
        else:                                  # JSONL: stream it
            yield json.loads(first + fh.readline())
            for line in fh:
                line = line.strip().rstrip(",")
                if line:
                    yield json.loads(line)


def load_oracle_tags(path):
    """Invert Scryfall's Oracle Tags file into {oracle_id: [tag, ...]}.

    The file is tag-centric: one object per tag, each holding a `taggings`
    array of the cards it applies to. We need the opposite direction, so we
    flip it. Art tags are skipped -- they key on illustration_id, not
    oracle_id, and describe artwork rather than function.

    Scryfall warns that slugs and labels are not permanent identifiers, so
    this is a rebuild-from-scratch join every refresh, never an incremental one.
    """
    by_card = {}
    tags = skipped = 0

    for t in iter_records(path):
        if t.get("type") != "oracle":
            skipped += 1
            continue
        # Prefer the stable slug; fall back to label, then name.
        name = t.get("slug") or t.get("label") or t.get("name")
        if not name:
            continue
        tags += 1
        for tagging in t.get("taggings") or []:
            oid = tagging.get("oracle_id")
            if oid:
                by_card.setdefault(oid, set()).add(name)

    print(f"loaded {tags:,} oracle tags covering {len(by_card):,} cards"
          + (f" ({skipped:,} non-oracle tags skipped)" if skipped else ""))
    return by_card


def main(src, dst, tags_src=None):
    tag_index = load_oracle_tags(tags_src) if tags_src else {}
    cards = {}
    seen = kept = 0

    for d in iter_records(src):
        seen += 1

        if d.get("lang") != "en":
            continue
        if d.get("digital"):
            continue
        if "paper" not in (d.get("games") or []):
            continue
        if d.get("layout") in DROP_LAYOUTS:
            continue
        if d.get("set_type") in DROP_SET_TYPES:
            continue
        if d.get("oversized"):
            continue

        key = d.get("oracle_id") or f"{d.get('name')}|{d.get('type_line')}|{d.get('layout')}"
        price = price_of(d)
        rarity = d.get("rarity")
        released = d.get("released_at")

        if key in cards:
            # merge printing-level info into the existing record
            c = cards[key]
            c["printings"] += 1
            if price is not None and (c["price"] is None or price < c["price"]):
                c["price"] = price
            if rarity and RARITY_ORDER.get(rarity, 9) < RARITY_ORDER.get(c["rarity"], 9):
                c["rarity"] = rarity
            # Earliest printing wins set, set_type and released TOGETHER, so
            # all three always describe the same physical printing.
            if released and (c["released"] is None or released < c["released"]):
                c["released"] = released
                c["set"] = d.get("set")
                c["set_type"] = d.get("set_type")
            # Older printings predate these fields; backfill from any printing.
            for opt in ("edhrec_rank", "penny_rank", "produced_mana"):
                if opt not in c and d.get(opt) not in (None, ""):
                    c[opt] = d[opt]
            if d.get("game_changer"):
                c["game_changer"] = True
            continue

        kept += 1
        rec = {
            "name": d.get("name"),
            "oracle_id": d.get("oracle_id"),
            "layout": d.get("layout"),
            "mana_cost": d.get("mana_cost") or "",
            "cmc": d.get("cmc"),
            "type_line": d.get("type_line") or "",
            "oracle_text": d.get("oracle_text") or "",
            "colors": d.get("colors") or [],
            "color_identity": d.get("color_identity") or [],
            "keywords": d.get("keywords") or [],
            "rarity": rarity,
            "set": d.get("set"),
            "set_type": d.get("set_type"),
            "released": released,
            "printings": 1,
            "price": price,
        }

        for opt in ("power", "toughness", "loyalty", "defense", "produced_mana",
                    "edhrec_rank", "penny_rank"):
            if d.get(opt) not in (None, ""):
                rec[opt] = d[opt]

        if d.get("reserved"):
            rec["reserved"] = True
        if d.get("game_changer"):
            rec["game_changer"] = True

        # multi-face cards carry their real text down here
        if d.get("card_faces"):
            rec["faces"] = [slim_face(f) for f in d["card_faces"]]

        # token / meld / combo references -- important for Horde deck building
        parts = d.get("all_parts")
        if parts:
            refs = [{"c": p.get("component"), "n": p.get("name")}
                    for p in parts if p.get("name") != d.get("name")]
            if refs:
                rec["parts"] = refs

        leg = d.get("legalities") or {}
        # store only non-legal statuses; absence means legal
        flags = {f: leg[f] for f in KEEP_FORMATS if leg.get(f) and leg[f] != "legal"}
        if flags:
            rec["not_legal"] = flags

        cards[key] = rec

    # Join tags on oracle_id. Sorted for stable, diff-friendly output.
    tagged = 0
    if tag_index:
        for rec in cards.values():
            found = tag_index.get(rec.get("oracle_id"))
            if found:
                rec["tags"] = sorted(found)
                tagged += 1

    # Alphabetical so git diffs between monthly refreshes stay readable.
    out = sorted(cards.values(),
                 key=lambda c: ((c["name"] or "").lower(), c["released"] or ""))
    with open_text(dst, "wt") as fh:
        for rec in out:
            fh.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")

    print(f"read {seen:,} printings -> wrote {kept:,} unique cards -> {dst}")
    if tag_index:
        pct = 100.0 * tagged / kept if kept else 0
        print(f"tagged {tagged:,} of {kept:,} cards ({pct:.1f}% coverage)")


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__.strip())
    main(*sys.argv[1:])
