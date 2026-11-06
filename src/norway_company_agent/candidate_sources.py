"""Ranked, bounded discovery leads, adapted from the reference discovery funnel.

Names, subunits and email domains supply candidates only. The central identity
gate must independently establish the legal entity after fetching each site.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .website import normalize_homepage, normalize_social_url

LEGAL = {"as", "asa", "ans", "da", "enk", "nuf", "sa", "sti", "ks", "ba", "brl", "bbl", "esek", "iks", "sf", "hf", "inc", "ltd", "limited", "stiftelsen", "stiftelse", "borettslag", "sameie"}
TAILS = {"products", "norge", "norway", "drift", "service", "services", "eiendom", "invest", "holding", "utleie", "consulting", "solutions", "teknikk", "partner", "handel", "industri", "gruppen", "group"}
GENERIC_FIRST = TAILS | {"sameiet", "borettslaget", "stiftelsen", "foreningen", "the"}
SHARED_MAIL = {"gmail.com", "hotmail.com", "outlook.com", "live.no", "live.com", "online.no", "yahoo.com", "icloud.com", "msn.com", "hotmail.no", "outlook.no"}


def words(value: Any, alternate: bool = False) -> list[str]:
    text = str(value or "").casefold().translate(str.maketrans({"ø": "oe" if alternate else "o", "å": "aa" if alternate else "a", "æ": "ae"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return [word for word in re.findall(r"[a-z0-9]+", text) if word not in LEGAL | {"og", "and", "the"}]


def domain_guesses(name: str, municipality: str = "", limit: int = 14) -> list[str]:
    tokens = words(name)
    if not tokens:
        return []
    ranked = []
    def add(stem: str, suffix: str, priority: int) -> None:
        if 3 <= len(stem) <= 60 and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", stem):
            ranked.append((priority, len(ranked), f"https://{stem}.{suffix}/"))
    joined = "".join(tokens)
    add(joined, "no", 100)
    if len(tokens) <= 4:
        add(joined, "com", 96)
    if len(tokens) > 1:
        add("-".join(tokens), "no", 94)
        if tokens[-1] in TAILS:
            add("".join(tokens[:-1]), "no", 98)
            add("".join(tokens[:-1]), "com", 92)
        places = words(municipality)
        if places and tokens[-len(places):] == places:
            add("".join(tokens[:-len(places)]), "no", 97)
        if tokens[0] not in GENERIC_FIRST:
            add(tokens[0], "no", 90)
            add(tokens[0], "com", 88)
        if len(tokens) >= 3:
            add("".join(tokens[:2]), "no", 91)
    if any(word in {"barnehage", "barnehager"} for word in tokens):
        shortened = ["bhg" if word in {"barnehage", "barnehager"} else word for word in tokens]
        add("-".join(shortened), "no", 95)
        add("".join(shortened), "no", 95)
    add(joined + "as", "no", 80)
    add(joined + "-as", "no", 79)
    alternative = "".join(words(name, alternate=True))
    if alternative != joined:
        add(alternative, "no", 93)
    result = []
    for _, _, url in sorted(ranked, key=lambda item: (-item[0], item[1])):
        if url not in result:
            result.append(url)
        if len(result) >= limit:
            break
    return result


def relationship_candidates(profile: dict, cached_subunits: list[dict] | None = None) -> list[tuple[str, str]]:
    locations = (profile.get("evidence", {}).get("locations", {}).get("value") or {}).get("locations") or []
    units = [*locations, *(cached_subunits or [])]
    output = []
    seen = set()
    for unit in units:
        for raw in (unit.get("website"),):
            url = normalize_homepage(raw)
            if url and not normalize_social_url(url) and url not in seen:
                output.append((url, "official_subunit_website")); seen.add(url)
        mail = str(unit.get("email") or "")
        host = mail.rsplit("@", 1)[-1].casefold() if "@" in mail else ""
        if host and host not in SHARED_MAIL and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
            url = f"https://{host}/"
            if url not in seen:
                output.append((url, "official_subunit_email")); seen.add(url)
    return output[:4]


def subunit_guesses(profile: dict, cached_subunits: list[dict] | None = None) -> list[str]:
    locations = (profile.get("evidence", {}).get("locations", {}).get("value") or {}).get("locations") or []
    units = [*locations, *(cached_subunits or [])]
    units.sort(key=lambda unit: -int(unit.get("employees") or 0))
    names = []
    output = []
    for unit in units:
        name = str(unit.get("name") or "")
        key = " ".join(words(name))
        if not key or key == " ".join(words(profile.get("name"))) or key in names:
            continue
        names.append(key)
        output.extend(domain_guesses(name, str(profile.get("municipality") or ""), limit=2))
        if len(names) >= 2:
            break
    return list(dict.fromkeys(output))


def operating_names(profile: dict, cached_subunits: list[dict] | None = None) -> list[str]:
    """Official parent-linked operating names are search leads, never ownership proof."""
    locations = (profile.get("evidence", {}).get("locations", {}).get("value") or {}).get("locations") or []
    units = [*locations, *(cached_subunits or [])]
    def employees(unit):
        try:
            return int(unit.get("employees") or 0)
        except (TypeError, ValueError):
            return 0
    units.sort(key=employees, reverse=True)
    seen = {" ".join(words(profile.get("name")))}
    names = []
    for unit in units:
        name = " ".join(str(unit.get("name") or "").split())
        key = " ".join(words(name))
        if len(key) < 4 or key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= 2:
            break
    return names
