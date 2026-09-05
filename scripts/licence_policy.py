#!/usr/bin/env python3
"""Fail closed on a shipped Python dependency whose licence is not on the allow-list.

Checked set: exactly the distributions `requirements.lock` installs into the
runtime image (Dockerfile's build stage, `--prefix=/install`), the same set
`supply-chain/licence-policy.yml` documents. `requirements-build.lock` is
build-stage only and `requirements-dev.lock` never ships, so neither is
checked here.

Standard library only: `pyyaml` is not among klove's dependencies (checked
`requirements-dev.lock`), so `parse_policy` below hand-parses the policy
file's two known keys instead of pulling in a YAML dependency for one file.

Usage, after installing `requirements.lock` into a clean target directory
(`python -m pip install --require-hashes --no-deps --target <dir> -r
requirements.lock` -- `--no-deps` is safe because the lock is hash-pinned and
therefore already a complete closure):

    python scripts/licence_policy.py --site <dir>
"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "supply-chain" / "licence-policy.yml"
_KLOVE_DISTRIBUTION = "klove"

# --- policy file -----------------------------------------------------------

_KNOWN_KEYS = frozenset({"allow-licenses", "allow-dependencies-licenses"})
_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z-]+):\s*$")
_TOP_LEVEL_EMPTY_LIST = re.compile(r"^([A-Za-z-]+):\s*\[\s*\]\s*$")
_LIST_ITEM = re.compile(r"^\s+-\s+(\S+)\s*$")
_PURL_PYPI_PREFIX = "pkg:pypi/"


@dataclass(frozen=True)
class LicencePolicy:
    """The two lists `supply-chain/licence-policy.yml` carries."""

    allow_licenses: frozenset[str]
    allow_exact_exceptions: frozenset[tuple[str, str]]


def normalize_distribution_name(name: str) -> str:
    """Fold a distribution name the way PyPI treats it as equivalent (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_purl_exception(purl: str) -> tuple[str, str]:
    """Parse an exact-version exception, `pkg:pypi/<name>@<version>`."""
    if not purl.startswith(_PURL_PYPI_PREFIX):
        raise ValueError(f"unsupported dependency exception PURL: {purl}")
    rest = purl[len(_PURL_PYPI_PREFIX) :]
    at_index = rest.rfind("@")
    if at_index <= 0:
        raise ValueError(f"dependency exception PURL is missing a version: {purl}")
    return normalize_distribution_name(rest[:at_index]), rest[at_index + 1 :]


def _strip_comment(line: str) -> str:
    hash_index = line.find("#")
    return line if hash_index == -1 else line[:hash_index]


def _require_known_key(key: str, raw_line: str) -> None:
    if key not in _KNOWN_KEYS:
        raise ValueError(f"licence policy has an unknown top-level key: {raw_line}")


def parse_policy(text: str) -> LicencePolicy:
    """Hand-parse the policy file's two known keys.

    Understood shape only: `allow-licenses` and `allow-dependencies-licenses`,
    each either an inline empty list (`key: []`) or a block of indented
    `- value` lines, with `#` comments anywhere. Fails if either key is
    missing, if a list item appears before any key, or on any other line
    shape -- there is no general YAML parser behind this.
    """
    allow_licenses: set[str] = set()
    allow_exact_exceptions: set[tuple[str, str]] = set()
    seen_keys: set[str] = set()
    current_key: str | None = None
    for raw_line in text.splitlines():
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue
        empty_list_match = _TOP_LEVEL_EMPTY_LIST.match(line)
        if empty_list_match:
            key = empty_list_match.group(1)
            _require_known_key(key, raw_line)
            seen_keys.add(key)
            current_key = None
            continue
        key_match = _TOP_LEVEL_KEY.match(line)
        if key_match:
            key = key_match.group(1)
            _require_known_key(key, raw_line)
            seen_keys.add(key)
            current_key = key
            continue
        item_match = _LIST_ITEM.match(line)
        if item_match:
            if current_key is None:
                raise ValueError(f"licence policy list item outside a known key: {raw_line}")
            if current_key == "allow-licenses":
                allow_licenses.add(item_match.group(1))
            else:
                allow_exact_exceptions.add(parse_purl_exception(item_match.group(1)))
            continue
        raise ValueError(f"unrecognised line in licence policy: {raw_line}")
    missing = _KNOWN_KEYS - seen_keys
    if missing:
        raise ValueError(f"licence policy is missing required key(s): {sorted(missing)}")
    return LicencePolicy(
        allow_licenses=frozenset(allow_licenses),
        allow_exact_exceptions=frozenset(allow_exact_exceptions),
    )


# --- SPDX licence expressions ----------------------------------------------


@dataclass(frozen=True)
class SpdxId:
    id: str


@dataclass(frozen=True)
class SpdxAnd:
    left: SpdxNode
    right: SpdxNode


@dataclass(frozen=True)
class SpdxOr:
    left: SpdxNode
    right: SpdxNode


SpdxNode = SpdxId | SpdxAnd | SpdxOr

_SPDX_TOKEN = re.compile(r"\(|\)|[^\s()]+")


def _tokenize_spdx_expression(expression: str) -> list[str]:
    return _SPDX_TOKEN.findall(expression)


class _SpdxParser:
    """Small recursive-descent parser: `id`, `WITH`, `AND`, `OR`, parens."""

    def __init__(self, tokens: Sequence[str]) -> None:
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> str | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _next(self) -> str:
        token = self._tokens[self._pos]
        self._pos += 1
        return token

    def parse(self) -> SpdxNode:
        node = self._parse_or()
        if self._pos != len(self._tokens):
            raise ValueError("trailing tokens in licence expression")
        return node

    def _parse_or(self) -> SpdxNode:
        node = self._parse_and()
        while self._peek() == "OR":
            self._next()
            node = SpdxOr(node, self._parse_and())
        return node

    def _parse_and(self) -> SpdxNode:
        node = self._parse_atom()
        while self._peek() == "AND":
            self._next()
            node = SpdxAnd(node, self._parse_atom())
        return node

    def _parse_atom(self) -> SpdxNode:
        symbol = self._peek()
        if symbol == "(":
            self._next()
            node = self._parse_or()
            if self._peek() != ")":
                raise ValueError("unbalanced parentheses in licence expression")
            self._next()
            return node
        if symbol is None or symbol in ("AND", "OR", ")"):
            raise ValueError("unexpected token in licence expression")
        self._next()
        if self._peek() == "WITH":
            self._next()
            exception_id = self._peek()
            if exception_id is None:
                raise ValueError("dangling WITH in licence expression")
            self._next()
            return SpdxId(f"{symbol} WITH {exception_id}")
        return SpdxId(symbol)


def parse_spdx_expression(expression: str) -> SpdxNode:
    """Parse an SPDX licence expression into a small AND/OR/id tree."""
    tokens = _tokenize_spdx_expression(expression)
    if not tokens:
        raise ValueError("empty licence expression")
    return _SpdxParser(tokens).parse()


def evaluate_spdx_expression(node: SpdxNode, allow_licenses: Iterable[str]) -> bool:
    """OR passes if any branch is allowed; AND needs every part allowed."""
    allowed = (
        allow_licenses if isinstance(allow_licenses, (set, frozenset)) else set(allow_licenses)
    )
    if isinstance(node, SpdxId):
        return node.id in allowed
    if isinstance(node, SpdxAnd):
        return evaluate_spdx_expression(node.left, allowed) and evaluate_spdx_expression(
            node.right, allowed
        )
    return evaluate_spdx_expression(node.left, allowed) or evaluate_spdx_expression(
        node.right, allowed
    )


def is_licence_allowed(expression: str, allow_licenses: Iterable[str]) -> bool:
    """True only for a parseable SPDX expression every required branch of which is allowed."""
    try:
        node = parse_spdx_expression(expression)
    except ValueError:
        return False
    return evaluate_spdx_expression(node, allow_licenses)


# --- resolving a licence from installed-distribution metadata --------------

# Real trove classifier suffixes (after "License :: OSI Approved :: ") mapped
# to the SPDX id they mean. "BSD License" is handled separately: PyPI's BSD
# classifier does not say which BSD variant, so it resolves only if the
# `License` field itself disambiguates (see `_disambiguate_bsd`); otherwise it
# fails closed.
_OSI_APPROVED_TO_SPDX = {
    "MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
}
_BSD_CLASSIFIER_SUFFIX = "BSD License"
_LICENSE_CLASSIFIER_PREFIX = "License :: OSI Approved :: "
_BSD_DISAMBIGUATION = (
    (re.compile(r"\b2[\s-]?Clause\b", re.IGNORECASE), "BSD-2-Clause"),
    (re.compile(r"\b3[\s-]?Clause\b", re.IGNORECASE), "BSD-3-Clause"),
)

# Last-resort normalisation for a free-text `License` field that names a
# well-known licence in a shape our SPDX-expression parser does not accept
# (extra words, punctuation) and no OSI classifier disambiguated either --
# metadata drift on the packager's part, not a policy question, so this maps
# the exact known spellings rather than adding an exception per package
# version. Matched case-insensitively with surrounding whitespace stripped;
# anything not listed here (including bare "BSD", "Apache", "GPL") still
# fails closed.
_FREE_TEXT_LICENSE_TO_SPDX = {
    "apache license 2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "mit license": "MIT",
    "mit": "MIT",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "isc license": "ISC",
}


class LicenceUnresolved(Exception):
    """Raised when no rule below can turn a distribution's metadata into an SPDX id.

    Carries the raw fields (`raw_summary`) so the caller can print them for a
    human to decide, per the module's fail-closed policy.
    """

    def __init__(self, raw_summary: str) -> None:
        super().__init__(raw_summary)
        self.raw_summary = raw_summary


@dataclass(frozen=True)
class DistributionLicenceInfo:
    """The exact metadata fields licence resolution reads, isolated for tests."""

    name: str
    version: str
    license_expression: str | None
    license_field: str | None
    classifiers: tuple[str, ...]


def distribution_licence_info(dist: importlib.metadata.Distribution) -> DistributionLicenceInfo:
    """Adapt a real `importlib.metadata.Distribution` to `DistributionLicenceInfo`."""
    metadata = dist.metadata
    return DistributionLicenceInfo(
        name=metadata["Name"],
        version=dist.version,
        license_expression=metadata.get("License-Expression"),
        license_field=metadata.get("License"),
        classifiers=tuple(metadata.get_all("Classifier") or ()),
    )


def _looks_like_spdx_expression(value: str) -> bool:
    try:
        parse_spdx_expression(value)
    except ValueError:
        return False
    return True


_VERSIONED_ID = re.compile(r"\d")
_BARE_NON_VERSIONED_IDS = frozenset({"MIT", "ISC"})


def _license_field_is_directly_recognisable(value: str) -> bool:
    """Stricter than `_looks_like_spdx_expression`, for the `License` field only.

    A genuine multi-part expression (AND/OR/parens) is trusted as-is. A lone
    atom is trusted only if it looks like a real SPDX short id -- versioned
    (contains a digit, e.g. "Apache-2.0", "BSD-3-Clause") or one of the few
    well-known bare exceptions ("MIT", "ISC"). Our SPDX-expression grammar
    would otherwise happily parse a bare "BSD", "Apache" or "GPL" as a lone
    id too -- syntactically valid, but none of those are real SPDX
    identifiers (the real ones are BSD-3-Clause, Apache-2.0, GPL-3.0-only,
    ...) -- so such a value is left for the free-text table or classifiers
    instead of being trusted directly here.
    """
    try:
        node = parse_spdx_expression(value)
    except ValueError:
        return False
    if isinstance(node, SpdxId):
        return bool(_VERSIONED_ID.search(node.id)) or node.id in _BARE_NON_VERSIONED_IDS
    return True


def _disambiguate_bsd(license_field: str | None) -> str | None:
    if not license_field:
        return None
    for pattern, spdx_id in _BSD_DISAMBIGUATION:
        if pattern.search(license_field):
            return spdx_id
    return None


def _normalise_free_text_license(license_field: str | None) -> str | None:
    if not license_field:
        return None
    return _FREE_TEXT_LICENSE_TO_SPDX.get(license_field.strip().lower())


def _resolve_from_classifiers(classifiers: Sequence[str], license_field: str | None) -> str | None:
    for classifier in classifiers:
        if not classifier.startswith(_LICENSE_CLASSIFIER_PREFIX):
            continue
        suffix = classifier[len(_LICENSE_CLASSIFIER_PREFIX) :]
        if suffix == _BSD_CLASSIFIER_SUFFIX:
            disambiguated = _disambiguate_bsd(license_field)
            if disambiguated is not None:
                return disambiguated
            continue
        mapped = _OSI_APPROVED_TO_SPDX.get(suffix)
        if mapped is not None:
            return mapped
    return None


def _raw_metadata_summary(info: DistributionLicenceInfo) -> str:
    license_classifiers = [c for c in info.classifiers if c.startswith(_LICENSE_CLASSIFIER_PREFIX)]
    return (
        f"License-Expression={info.license_expression!r} License={info.license_field!r} "
        f"classifiers={license_classifiers!r}"
    )


def resolve_licence(info: DistributionLicenceInfo) -> str:
    """Resolve one distribution's SPDX licence expression, or fail closed.

    Preference order: PEP 639 `License-Expression`; else a `License` field
    that is itself a directly recognisable SPDX id/expression (see
    `_license_field_is_directly_recognisable`); else a `License :: OSI
    Approved ::` classifier mapped through the small explicit table above;
    else the free-text `License` normalisation table above. Anything else
    raises `LicenceUnresolved` carrying the raw fields.
    """
    if info.license_expression:
        if _looks_like_spdx_expression(info.license_expression):
            return info.license_expression.strip()
        raise LicenceUnresolved(_raw_metadata_summary(info))

    if info.license_field and _license_field_is_directly_recognisable(info.license_field):
        return info.license_field.strip()

    mapped = _resolve_from_classifiers(info.classifiers, info.license_field)
    if mapped is not None:
        return mapped

    normalised = _normalise_free_text_license(info.license_field)
    if normalised is not None:
        return normalised

    raise LicenceUnresolved(_raw_metadata_summary(info))


# --- checking an installed site directory -----------------------------------


def check_site(site_dir: Path, policy: LicencePolicy) -> tuple[int, list[str]]:
    """Check every distribution installed under `site_dir` against `policy`.

    Returns `(checked_count, offender_lines)`; `offender_lines` is empty when
    every checked distribution's licence is allowed. The `klove` distribution
    itself, if present, is never checked -- it is the product, not a shipped
    dependency.
    """
    offenders: list[str] = []
    checked = 0
    for dist in importlib.metadata.distributions(path=[str(site_dir)]):
        info = distribution_licence_info(dist)
        if normalize_distribution_name(info.name) == _KLOVE_DISTRIBUTION:
            continue
        checked += 1
        exception_key = (normalize_distribution_name(info.name), info.version)
        if exception_key in policy.allow_exact_exceptions:
            continue
        try:
            expression = resolve_licence(info)
        except LicenceUnresolved as exc:
            offenders.append(f"{info.name}=={info.version}  {exc.raw_summary}  → not allowed")
            continue
        if is_licence_allowed(expression, policy.allow_licenses):
            continue
        offenders.append(f"{info.name}=={info.version}  {expression}  → not allowed")
    return checked, offenders


# --- CLI ---------------------------------------------------------------------


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--site",
        required=True,
        type=Path,
        help="directory requirements.lock was pip-installed --target into",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=_DEFAULT_POLICY,
        help="path to the licence policy YAML (default: supply-chain/licence-policy.yml)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    policy = parse_policy(args.policy.read_text(encoding="utf-8"))
    checked, offenders = check_site(args.site, policy)
    if offenders:
        for line in offenders:
            print(line)
        return 1
    print(f"{checked} distribution(s) checked against the licence policy; all allowed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
