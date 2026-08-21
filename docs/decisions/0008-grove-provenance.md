# ADR 0008: use a clean-room Grove compatibility boundary

Status: accepted

Date: 2026-08-21

## Context

ADR 0006 defines one narrow Grove integration, but it does not grant permission
to copy Grove implementation or visual material into Klove. This decision
records the source and licence evidence reviewed for #11 — Grove provenance and
sets the boundary for #32 — Grove bridge, #59 — embedded setup/recovery, and
#60 — minimal Grove contribution.

The supported upstream is
[`EdwardChamberlain/grove-control`](https://github.com/EdwardChamberlain/grove-control)
on `main` at commit `cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4`, which was also
the remote `HEAD` when rechecked on 2026-08-21. Compatibility claims apply only
to this revision until an explicit source-update review changes the pin.

The revision has a repository-root GNU Affero General Public License version 3
text and describes itself as `AGPL-3.0`. It has no observed per-source-file
SPDX identifiers, licence exceptions, CLA, or DCO. Its contribution guide
requires prior issue agreement and assignment, a fork, a focused pull request
to `dev`, tests, and companion user documentation for visible changes. GitHub's
applicable terms use an inbound-equals-outbound rule for contributions to a
repository with a licence notice.

The revision also:

- acknowledges that Grove is based on Bambuddy;
- records separate notices for vendored PrettyGCode, three.js, jQuery, dat.GUI,
  camera-controls, and a font under `gcode_viewer/VENDORED.md`;
- ships Grove logos, wordmarks, screenshots, printer images, icons, fonts,
  exact theme tokens, layout code, and wording without a reusable per-asset
  grant beyond the repository-wide declaration; and
- has a locked npm graph, but range-based Python requirements. The npm lock
  records permissive licences plus choice or attribution licences including
  `CC-BY-4.0`, `(MIT OR GPL-3.0-or-later)`, `(MIT OR CC0-1.0)`, and
  `(MIT AND Zlib)`. Grove's dependency graph is therefore evidence, not a
  dependency set for Klove to mirror.

AGPL section 5 requires a modified work conveyed as source to be licensed as a
whole under that licence, and section 13 adds a corresponding-source offer for
modified network software. Klove currently declares no project licence and its
dependency policy does not allow AGPL dependencies. Copying or adapting Grove's
virtual-printer code would therefore create a material licensing and release
change outside this issue.

## Decision

### Implementation source boundary

1. Klove will implement its MQTT/TLS and FTPS compatibility facades
   independently. No Grove source, source-derived pseudocode, tests, test data,
   message templates, certificates, comments, source identifiers, control flow,
   generated bundle, or virtual-printer asset may be copied, translated,
   traced, or adapted into Klove.
2. Grove and Klove remain separate programs communicating through documented
   network protocols. Grove may be run as an exact-revision black-box contract
   client. Its repository is not a Klove build input, package dependency,
   submodule, image layer, release asset, or source-generation input.
3. Adapting Grove code remains prohibited unless a later ADR records either a
   compatible Klove licensing decision or written permission covering the exact
   material and use. Attribution alone is not permission.

### Clean-room contract procedure

4. A compatibility observer may operate an unmodified checkout or image at the
   pinned Grove revision in an isolated test network using generated, obviously
   fake credentials and non-production data. The observer records only
   externally observable facts needed by the accepted Klove contract: transport
   negotiation, topic/path names, field types and bounds, ordering, and accepted
   or rejected behavior.
5. The observer must not provide Grove source, screenshots of source, copied
   literals that are not required protocol tokens, source structure, function
   names, prose, or implementation suggestions to a Klove implementer. A person
   who has inspected Grove's affected virtual-printer implementation may not be
   the clean-room implementer of the corresponding Klove component.
6. The implementer receives only ADRs, public protocol standards, independently
   written schemas, and approved black-box fixtures. The implementation must be
   authored without Grove source access. Its review records the implementer's
   isolation statement and compares behavior, never source shape.
7. Every committed Grove contract fixture has a sibling provenance manifest
   containing:

   - upstream repository and exact 40-character commit;
   - observed Grove component and black-box scenario;
   - capture harness revision, command, tool versions, and UTC capture date;
   - generated-data declaration and sanitization/normalization rules;
   - fixture media type, byte count, and SHA-256;
   - reviewer confirmation that it contains no credential, personal data,
     source excerpt, authored error prose, certificate/private key, or copied
     upstream fixture; and
   - a classification of each retained value as a required protocol token or
     externally observed fact.

   Raw traces are temporary, access-restricted, secret-scanned, and destroyed
   after the minimal fixture and manifest are approved. A fixture without its
   valid manifest fails the test gate.
8. Generated payloads should be preferred to captured payloads. Boundary tests
   must include rejection cases derived independently from the schema rather
   than copied from Grove tests. A revision update invalidates the compatibility
   claim until the clean-room contract lane passes again and its manifests name
   the new revision. The existing `tests/fixtures/grove/` command-decoder files
   are Klove protocol fixtures, not accepted Grove observation evidence; they
   must be replaced or receive the required classification before a test cites
   them as Grove compatibility evidence.

### Visual and textual material

9. Klove will not reuse, embed, transform, trace, or redistribute Grove logos,
   wordmarks, favicons, screenshots, printer/AMS images, icons, fonts, CSS,
   exact theme token sets, component markup, layout compositions, animation,
   or authored help/error prose.
10. The setup surface may be visually coherent with its host through an
    independently authored semantic design brief: accessible accent, surface,
    text, focus, success, warning, and error roles; system fonts; responsive
    cards/forms; and WCAG-tested contrast. It must not be a pixel match or use
    Grove's exact palette, spacing scale, shadows, typography scale, or DOM/CSS
    structure. “Grove-themed” in ADR 0006 means this independent visual
    coherence, not copied trade dress or assets.
11. Generic interaction patterns and factual interoperability labels may be
    independently implemented. The exact product labels and completion fields
    already specified by ADR 0006, including **Klipper via Klove** and `KLOVE`,
    are Klove contract material. New user-facing prose is written independently.
12. A third-party icon, font, or other asset may be added only from its original
    publisher, at an exact version, under a Klove-allowed licence, with required
    attribution and an SBOM entry. An asset found through Grove is not copied
    from Grove. The Grove name is used only as needed to describe
    interoperability; Klove does not use Grove branding or imply endorsement.

### Upstream contribution

13. The #60 — minimal Grove contribution is new Grove-side code, authored in a
    short-lived fork from the pinned supported revision. Before implementation,
    its contributor must obtain upstream issue agreement and assignment as
    required by `CONTRIBUTING.md`.
14. The contribution is submitted through GitHub under Grove's repository
    licence terms, subject to any separate terms upstream introduces. The
    contributor must own or be authorized to submit every line and asset,
    retain required authorship information, and must not import Klove code or
    third-party material into Grove without an explicit compatible licence.
    Its `KLOVE` icon is original, does not adapt Grove or Bambu imagery, and is
    expressly submitted under Grove's terms. Any reuse of that icon by Klove
    requires separately recorded permission or dual licensing. No additional
    dependency is added unless upstream requests and reviews it.
15. The pull request targets upstream's documented `dev` branch, includes the
    required tests and companion documentation, and identifies its issue. Klove
    may test the public upstream pull-request commit during review, but neither
    a private patch nor a fork becomes a supported runtime dependency.
16. Upstream acceptance advances the supported revision only after Klove's
    exact-revision contract and browser lanes pass. Rejection or delay leaves
    standalone Klove recovery and compatibility development available, but
    does not authorize a persistent fork, copied implementation, a fake Bambu
    model, or a compatibility claim for the unmerged feature.

### Notices, SBOM, and release gates

17. Klove's source and release artifacts must not claim that Grove code or
    assets are included. Documentation and fixture manifests identify Grove as
    an interoperability reference with its repository, exact revision, and
    declared licence.
18. Any included third-party dependency or asset must have an exact source,
    version/digest, licence expression, copyright/attribution notice, and
    inclusion purpose. Required licence texts and notices ship with the source
    and applicable artifact. Unknown, missing, disallowed, or ambiguous licence
    metadata fails closed pending review.
19. Dependency review remains required on every dependency change. The release
    SPDX SBOM must cover application packages, runtime Python dependencies,
    browser packages, fonts, icons, and vendored files. A pre-release licence
    check compares the SBOM with the allowed-licence policy and required-notice
    inventory; generating an SBOM without evaluating it is insufficient.
20. Each compatibility release records the Klove commit, supported Grove
    commit, fixture-manifest hashes, dependency/SBOM result, and upstream Grove
    contribution status. Drift in any identity blocks promotion until reviewed.

## Consequences

- The Grove virtual-printer implementation remains a useful black-box oracle,
  but it does not shorten Klove implementation by source reuse.
- Klove avoids an implicit AGPL relicensing decision and keeps its release
  composition auditable.
- The embedded surface uses independent Klove visual assets and wording. It can
  feel consistent without copying Grove expression or branding.
- Contract capture needs an observer who is isolated from the corresponding
  implementation. This costs more initially but preserves a defensible source
  boundary and repeatable provenance.
- A future decision may adopt compatible licensing or accept specific written
  permission, but it must reassess notices, source delivery, dependencies, and
  the complete release before any Grove-derived material enters Klove.

## Validation

- Repository policy rejects Grove source or asset paths, Grove package/archive
  inputs, and fixtures without complete provenance manifests.
- Secret and privacy checks cover both raw-capture handling and committed
  fixtures.
- Browser tests verify independently authored responsive and accessible UI; no
  screenshot baseline may be copied from Grove.
- Exact-revision Grove contract tests, licence/dependency review, notice checks,
  and the release SBOM gate must pass before a compatibility claim.

## Primary evidence

- [Grove revision](https://github.com/EdwardChamberlain/grove-control/tree/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4)
- [Grove licence at the reviewed revision](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/LICENSE)
- [Grove contribution guide at the reviewed revision](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/CONTRIBUTING.md)
- [Grove acknowledgement of Bambuddy](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/README.md)
- [Grove vendored notices](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/gcode_viewer/VENDORED.md)
- [Grove Python requirements](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/requirements.txt)
- [Grove npm manifest](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/frontend/package.json)
- [Grove npm lock](https://github.com/EdwardChamberlain/grove-control/blob/cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4/frontend/package-lock.json)
- [GNU AGPL version 3](https://www.gnu.org/licenses/agpl-3.0.html)
- [GNU licence FAQ](https://www.gnu.org/licenses/gpl-faq.html)
- [GitHub contributions-under-repository-licence terms](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#6-contributions-under-repository-license)

## Tracking

- [#11 — Grove provenance](https://github.com/tomlawesome/klove/issues/11)
- [#32 — Grove bridge](https://github.com/tomlawesome/klove/issues/32)
- [#59 — embedded setup/recovery](https://github.com/tomlawesome/klove/issues/59)
- [#60 — minimal Grove contribution](https://github.com/tomlawesome/klove/issues/60)
