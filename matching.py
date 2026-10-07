"""Matching what people type against known names.

Used for two things: resolving the names a command was given (labels, people, buckets, plans)
and producing autocomplete suggestions, including for options that take several names
separated by commas. Pure functions, no I/O.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence, TypeVar

T = TypeVar("T")

_WORD_BREAK_RE = re.compile(r"[\s,.@_\-/]+")


def name_key(text: str) -> str:
    """Key for comparing names: ignores case and spacing ("Kowalski,  Jan" == "kowalski, jan")."""
    collapsed = " ".join(text.split())
    return re.sub(r"\s*,\s*", ",", collapsed).casefold()


def split_names(raw: str, known: Iterable[str] = ()) -> list[str]:
    """Split a typed list such as "Bug, Frontend" into names.

    Commas and semicolons both separate entries. A comma that is part of a known name
    ("Kowalski, Jan") does not split it: the longest run of parts that forms a known name
    is kept together. Duplicates are dropped, order is kept.
    """
    known_keys = {name_key(name) for name in known}
    names: list[str] = []
    for chunk in raw.split(";"):
        parts = [part.strip() for part in chunk.split(",")]
        index = 0
        while index < len(parts):
            if not parts[index]:
                index += 1
                continue
            for end in range(len(parts), index, -1):
                candidate = ", ".join(parts[index:end])
                if end - index == 1 or name_key(candidate) in known_keys:
                    names.append(candidate)
                    index = end
                    break
    unique: dict[str, str] = {}
    for name in names:
        unique.setdefault(name_key(name), name)
    return list(unique.values())


def rank_matches(query: str, items: Sequence[T], keys: Callable[[T], Iterable[str]]) -> list[T]:
    """Items whose keys match what was typed, best first: exact, prefix, word prefix, substring."""
    wanted = name_key(query)
    if not wanted:
        return list(items)
    scored: list[tuple[int, int, T]] = []
    for position, item in enumerate(items):
        best: Optional[int] = None
        for raw in keys(item):
            key = name_key(raw)
            if not key:
                continue
            if key == wanted:
                rank = 0
            elif key.startswith(wanted):
                rank = 1
            elif any(word.startswith(wanted) for word in _WORD_BREAK_RE.split(key)):
                rank = 2
            elif wanted in key:
                rank = 3
            else:
                continue
            best = rank if best is None else min(best, rank)
        if best is not None:
            scored.append((best, position, item))
    scored.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _, _, item in scored]


@dataclass(frozen=True)
class ListCandidate:
    """One selectable entry of a multi-value option."""

    token: str  # the text that ends up in the option
    keys: tuple[str, ...] = ()  # other spellings people may type to find it (an email, say)


def suggest_list(
    current: str, candidates: Sequence[ListCandidate], *, limit: int = 25, max_length: int = 100
) -> list[str]:
    """Autocomplete for an option that takes several names separated by commas.

    Everything before the last separator is kept; the part being typed is completed. Each
    suggestion is the whole new option text ("Bug, Fro" -> "Bug, Frontend"), because picking
    a suggestion in Discord replaces the entire option.
    """
    by_key: dict[str, ListCandidate] = {}
    for candidate in candidates:
        for spelling in (candidate.token, *candidate.keys):
            if spelling:
                by_key.setdefault(name_key(spelling), candidate)
    spellings = [spelling for candidate in candidates for spelling in (candidate.token, *candidate.keys) if spelling]

    def matches_for(chosen: Sequence[str], fragment: str) -> list[ListCandidate]:
        taken = {
            name_key(by_key[name_key(name)].token) if name_key(name) in by_key else name_key(name) for name in chosen
        }
        available = [c for c in candidates if name_key(c.token) not in taken]
        return rank_matches(fragment, available, lambda c: (c.token, *c.keys))

    cuts = [position for position, char in enumerate(current) if char in ",;"]
    chosen: list[str] = []
    fragment = current
    # Take the latest separator whose left-hand side consists only of known names. Earlier
    # separators may belong to a name that is still being typed ("Kowalski, J").
    for cut in reversed(cuts):
        head = split_names(current[:cut], spellings)
        if head and all(name_key(name) in by_key for name in head):
            chosen, fragment = head, current[cut + 1 :]
            break
    matches = matches_for(chosen, fragment)
    if not matches and cuts and not chosen:
        # The earlier entries contain a typo. Still complete the last one; the typo is
        # reported when the command runs.
        chosen = split_names(current[: cuts[-1]], spellings)
        matches = matches_for(chosen, current[cuts[-1] + 1 :])

    canonical = [by_key[name_key(name)].token if name_key(name) in by_key else name for name in chosen]
    suggestions: list[str] = []
    for match in matches:
        tokens = [*canonical, match.token]
        separator = "; " if any("," in token for token in tokens) else ", "
        text = separator.join(tokens)
        if len(text) <= max_length:
            suggestions.append(text)
        if len(suggestions) >= limit:
            break
    return suggestions


def listing(names: Iterable[str], limit: int = 12) -> str:
    """ "A, B, C and 4 more" for error messages."""
    names = list(names)
    shown = ", ".join(names[:limit])
    return f"{shown} and {len(names) - limit} more" if len(names) > limit else shown


def did_you_mean(wanted: str, options: Iterable[str]) -> str:
    lookup = {option.casefold(): option for option in options if option}
    close = difflib.get_close_matches(wanted.casefold(), list(lookup), n=1, cutoff=0.6)
    return f" Did you mean “{lookup[close[0]]}”?" if close else ""
