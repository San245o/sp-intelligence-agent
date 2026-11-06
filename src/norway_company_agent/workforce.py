"""Standalone annual-report workforce extraction; excludes group counts and ambiguous matches."""
from __future__ import annotations
import re

OCR_NUMBER = r"(?-i:\b[0-9O][0-9O .,-]{0,8})"
PATTERNS = (
    (0, "full_time_equivalents", re.compile(rf"(?i)(?:antall|tal\s+p[aå])\s+(?:aarsverk|arsverk|årsverk)\s+i\s+(?:regnskapsaret|rekneskapsaret)\s*(?:er|:|=)?\s*({OCR_NUMBER})")),
    (0, "full_time_equivalents", re.compile(rf"(?i)antall\s+(?:aarsverk|arsverk|årsverk)(?:\s+sysselsatt\s+i\s+regnskapsaret)?\s*(?:er|:|=)?\s*({OCR_NUMBER})")),
    (0, "full_time_equivalents", re.compile(rf"(?i)selskapet\s+har(?:\s+[i1]\s+\d{{4}})?\s+sysselsatt\s+({OCR_NUMBER})\s+(?:aarsverk|arsverk|årsverk)")),
    (0, "full_time_equivalents", re.compile(rf"(?i)selskapet\s+har\s+({OCR_NUMBER})\s+(?:aarsverk|arsverk|årsverk)")),
    (0, "full_time_equivalents", re.compile(rf"(?i)antall\s+(?:aarsverk|arsverk|årsverk)\s+(?:sysselsatt|syssetsatt)\s+i\s+regnskapsaret\s*(?:er|:|=)?\s*({OCR_NUMBER})")),
    (1, "employees", re.compile(rf"(?i)gjennomsnittlig(?:e)?\s+antall\s+ansatte(?:\s+i\s+regnskapsaret)?\s*(?:er|:|=)?\s*({OCR_NUMBER})")),
    (1, "employees", re.compile(rf"(?i)antall\s+ansatte\s*(?:er|:|=)?\s*({OCR_NUMBER})")),
    (2, "employees", re.compile(rf"(?i)({OCR_NUMBER})\s+(?:heltids)?ansatte\b")),
)
WORD_COUNTS = {"ingen": 0, "en": 1, "ett": 1, "to": 2, "tre": 3, "fire": 4, "fem": 5}
WORD_EMPLOYEE_PATTERN = re.compile(
    r"(?i)\b(?:det\s+er|selskapet\s+har)\s+(ingen|en|ett|to|tre|fire|fem)\s+ansatte\b"
)
ZERO_WORKFORCE_PATTERN = re.compile(
    r"(?i)\b(?:selskapet|stiftelsen|legatet|sameiet|det)\s+"
    r"(?:har\s+ingen\s+(ansatte|(?:aarsverk|arsverk|årsverk))|"
    r"har\s+ikke\s+hatt\s+(?:noen\s+)?ansatte|"
    r"hadde\s+ingen\s+ansatte|"
    r"ikke\s+har\s+ansatte)\b"
)
WORKFORCE_TERMS = re.compile(r"(?i)ansatt|aarsverk|arsverk|årsverk|sysselsatt")


def needs_ocr(text: str) -> bool:
    """OCR image-heavy reports even when a small machine-readable cover exists."""
    return len(text.strip()) < 100 or not WORKFORCE_TERMS.search(text)


def number_value(value: str) -> int | float | None:
    cleaned = value.replace(" ", "").replace("O", "0").strip(".,-")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif re.search(r"\.\d{1,2}$", cleaned):
        pass
    else:
        cleaned = cleaned.replace(".", "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if not 0 <= number <= 100_000:
        return None
    return int(number) if number.is_integer() else round(number, 2)


def extract_candidate(text: str) -> tuple[int | float | None, str | None, str, str | None]:
    compact = re.sub(r"[\t\r ]+", " ", text)
    matches = []
    for priority, measure, pattern in PATTERNS:
        for match in pattern.finditer(compact):
            start = max(0, compact.rfind("\n", 0, match.start()) + 1)
            end_pos = compact.find("\n", match.end())
            end = len(compact) if end_pos < 0 else end_pos
            span = compact[start:end].strip()[:500]
            if re.search(r"(?i)konsern|group", span):
                continue
            count = number_value(match.group(1))
            if count is not None:
                matches.append((priority, count, span, measure))
    for match in WORD_EMPLOYEE_PATTERN.finditer(compact):
        start = max(0, compact.rfind("\n", 0, match.start()) + 1)
        end_pos = compact.find("\n", match.end())
        end = len(compact) if end_pos < 0 else end_pos
        span = compact[start:end].strip()[:500]
        if not re.search(r"(?i)konsern|group", span):
            matches.append((2, WORD_COUNTS[match.group(1).casefold()], span, "employees"))
    for match in ZERO_WORKFORCE_PATTERN.finditer(compact):
        start = max(0, compact.rfind("\n", 0, match.start()) + 1)
        end_pos = compact.find("\n", match.end())
        end = len(compact) if end_pos < 0 else end_pos
        span = compact[start:end].strip()[:500]
        if not re.search(r"(?i)konsern|group", span):
            measure = "full_time_equivalents" if match.group(1) and re.search(r"(?i)verk", match.group(1)) else "employees"
            matches.append((0, 0, span, measure))
    if not matches:
        return None, None, "no_employee_phrase", None
    best_priority = min(item[0] for item in matches)
    best = [item for item in matches if item[0] == best_priority]
    values = {item[1] for item in best}
    if len(values) != 1:
        return None, None, "conflicting_employee_counts", None
    chosen = sorted(best, key=lambda item: item[3] != "full_time_equivalents")[0]
    return chosen[1], chosen[2], "accepted", chosen[3]
