"""Checks every line the AI writes before anyone sees it."""

import re

SENSITIVE = re.compile(
    r"\b(hungover|drunk|wasted|weed|vape|therap(y|ist)|rehab|depress\w*|anxiety|suicid\w*|self.?harm|"
    r"pregnan\w*|ex|breakup|broke up|divorc\w*|church|mosque|temple|religio\w*|politic\w*|trump|biden|"
    r"democrat\w*|republican\w*|salary|debt|rent money|immigra\w*)\b",
    re.IGNORECASE,
)
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")


def is_sensitive(text: str) -> bool:
    return bool(SENSITIVE.search(text))


def names_in(text: str, names: list[str]) -> set[str]:
    lowered = text.lower()
    return {n for n in names if re.search(rf"\b{re.escape(n.lower())}\b", lowered)}


def check_host_line(
    line: str, target: str, all_names: list[str], max_words: int = 25
) -> str | None:
    """None if the line is fine, otherwise the reason it was rejected."""
    if not line or not line.strip():
        return "empty"
    if len(line.split()) > max_words:
        return "too long"
    if target not in names_in(line, all_names):
        return "doesn't hand the turn to the target"
    if names_in(line, all_names) - {target}:
        return "names someone other than the target"
    if is_sensitive(line):
        return "sensitive topic"
    if len(EMOJI.findall(line)) > 1:
        return "too many emojis"
    return None


def check_round_text(text: str, max_chars: int = 240) -> str | None:
    if not text or not text.strip():
        return "empty"
    if len(text) > max_chars:
        return "too long"
    if is_sensitive(text):
        return "sensitive topic"
    return None
