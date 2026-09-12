"""Shared text normalization for lexical matching + direct lookup.

One place to keep the "how do we compare a question phrase to a stored
skill / technology / employer / etc." rules. The rules are deliberately
narrow — this is not a natural-language question dictionary:

  1. Lowercase and squash any non-alphanumeric character to a single
     space, so slashes / dashes / commas / dots don't break matching.
     "CI/CD Pipelines" → "ci cd pipelines"; "nl-to-sql" → "nl to sql".

  2. Apply a SMALL alias map for well-known technology abbreviations
     ("k8s" → "kubernetes", "aws" → "amazon web services", ...). The
     RHS values are the canonical space-separated forms.

  3. Optional token list, with common English stopwords removed. Used
     by the BM25 tokenizer so query weight lands on distinctive terms.

Callers:
  - domain/profile/direct_lookup.py — normalized whole-word matching.
  - retrieval/lexical.py            — BM25 tokenization.
  - retrieval/hybrid.py             — metadata-entity match.
"""

from __future__ import annotations

import re


# Canonical form on the right is always space-separated and lowercase.
# The alias map is intentionally small and entity-focused. Do NOT add
# natural-language paraphrases here; the LLM / retrieval layer handles
# those.
TECH_ALIASES: dict[str, str] = {
    # cloud
    "aws": "amazon web services",
    "gcp": "google cloud platform",
    # kubernetes
    "k8s": "kubernetes",
    # databases
    "postgres": "postgresql",
    "psql": "postgresql",
    # languages
    "js": "javascript",
    "ts": "typescript",
    # llm / rag / nl2sql — canonicalize to a few common spellings
    "llm": "large language model",
    "llms": "large language model",
    "rag": "retrieval augmented generation",
    "nl2sql": "nl to sql",
    "nl to sql": "nl to sql",
    # ci/cd — the punctuation stripper already normalizes the slash to
    # a space, so "ci cd" is the surface form. Keep "cicd" too for
    # sites that concatenate the letters.
    "cicd": "ci cd",
    "ci cd": "ci cd",
}


# Short English stopword list. Only words that are useless for
# distinguishing between profile documents.
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "in", "on", "at", "to", "from", "by",
    "with", "as", "and", "or", "but", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "you",
    "your", "yours", "me", "my", "mine", "he", "she", "it", "they",
    "them", "their", "this", "that", "these", "those", "what", "which",
    "who", "whom", "how", "when", "where", "why", "if", "any", "some",
    "will", "would", "could", "should", "can", "may", "might", "must",
    "not", "no", "yes", "tell", "about", "get", "got", "give", "let",
    "know", "used", "using", "use", "worked", "work", "working", "built",
    "build", "building", "experience", "please",
})


_ALIAS_KEYS_LONGEST_FIRST = sorted(TECH_ALIASES.keys(), key=lambda k: (-len(k), k))


def normalize_text(text: str) -> str:
    """Return a normalized surface form suitable for whole-word or
    token-level comparison. Idempotent on already-normalized input."""
    if not text:
        return ""
    s = text.lower()
    # Aggressive punctuation strip. Anything non-alphanumeric becomes a
    # single space; keeps aliasing simple downstream.
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Longest-first alias substitution using word boundaries. Applying
    # longest-first ensures "nl to sql" wins over "nl" if both were
    # ever aliased.
    for k in _ALIAS_KEYS_LONGEST_FIRST:
        v = TECH_ALIASES[k]
        if k == v:
            # Identity mapping — nothing to substitute.
            continue
        s = re.sub(rf"(?:(?<=\s)|^){re.escape(k)}(?=\s|$)", v, s)

    return re.sub(r"\s+", " ", s).strip()


def tokenize(text: str) -> list[str]:
    """Normalized token list with stopwords dropped. Used by BM25."""
    return [t for t in normalize_text(text).split() if t and t not in _STOPWORDS]


def contains_term(query: str, stored: str) -> bool:
    """Whole-word/bidirectional match after normalization. True if the
    normalized query and normalized stored strings share their content
    as a contiguous phrase in either direction. Empty inputs → False."""
    q = normalize_text(query)
    s = normalize_text(stored)
    if not q or not s:
        return False
    if q == s:
        return True
    return bool(
        re.search(rf"(?:^|\s){re.escape(q)}(?=\s|$)", s)
        or re.search(rf"(?:^|\s){re.escape(s)}(?=\s|$)", q)
    )
