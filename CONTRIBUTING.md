# Adopting, and contributing

The README explains this capability to people. This page primarily serves an
agent evaluating an integration or preparing a change on a maintainer's behalf.
Use the Adopting section to establish fit, guarantees, costs and evidence; use
Contributing to locate the change and verify it. Contracts remain authoritative.

## Adopting

**Start here: [what adopting actually involves](https://openabstractions.org/adopt.html)**
— examples of calling and implementing capabilities, with the environments and
versions used to check them. For installation details, follow this repository's
language-specific README at the revision you intend to adopt.

Before recommending adoption, establish:

- The existing application's requirement and the contract rules that satisfy it.
- The language package and provider, including what needs to be installed or kept
  running, and what happens when that provider is unavailable.
- The pinned revision and required dependencies; verify the documented entry
  point from a clean consumer outside this repository's development workspace.
- The evidence for the target language and platform, and any remaining gaps.

Report these findings with source paths, revisions and the checks actually run.
Distinguish a build, a passing example and a conformance result. Report a mismatch
as a reason to defer or narrow the integration; do not invent a guarantee.

### Service-client acceptance checklist

For a facade integration, compile the README example as an outside consumer.
Resolve through the runtime bootstrap, invoke the selected typed client, then
repeat with an isolated absent bootstrap and require an explicit failure. Check
that the application creates no provider-owned files. Keep provider deployment
and application dependencies separate. Go's primary facade and `/client` should
have service-client dependencies.

For durable work, preserve request identity, logical owner and negotiated
promises across lost replies. Reconcile at the original binding. Record refusal,
unknown acceptance and terminal failure distinctly. A cancelled wait supplies no
proof that work was cancelled.

Return a concise integration report: chosen revisions, required runtime,
capability/guarantees, compiled example, exercised success and refusal, remaining
platform limits. Avoid marking a historical coverage record as a current run.

### Is it proven?

[The coverage grid](https://openabstractions.org/coverage.html) says which
implementation of which layer carries a verdict, in which language, on which
platform. Read the revision and scope attached to the verdict. `UNPROVEN`
marks a scenario the runner could not reach on this machine, never a pass or
a fail; it, an unsupported capability and a passing result have different
meanings.

A layer's own tests are a weaker claim than a conformance verdict. Both are
weaker than the same behaviour proven across two languages.

### What is it promising?

Each layer's `CONTRACT.md` — tagged rules, `[DL-R28]`, `[JOB-L5]` — is the
normative page; each tag names one numbered rule on that layer's contract
page, so a scenario can cite the exact rule it tests.
The README's Status section is where the refusals are: what is not supported,
what is refused rather than silently downgraded, and what is not measured.

The rules are not copied into any other repository on purpose. A normative page
in two places is a reader who cannot tell which one binds them.

### Judge an implementation yourself, including ours

[`conformance/`](https://github.com/openabstractions/abstractions/tree/main/conformance)
needs a POSIX shell and a driver program of your own; our source tree, our build
and Go are not required.
[`conformance/DRIVER.md`](https://github.com/openabstractions/abstractions/blob/main/conformance/DRIVER.md)
is the whole contract that program keeps. `conformance/selftest.sh` checks the
runner against drivers that are wrong in three different ways, before you trust
what it says about yours. Out of reach is never a pass.

### What is the evidence?

[`docs/results/`](https://github.com/openabstractions/abstractions/tree/main/docs/results),
indexed in
[`docs/results/README.md`](https://github.com/openabstractions/abstractions/blob/main/docs/results/README.md)
with the script that produced each transcript and the state of the machine that
ran it. They are our own output on our own machines: a record of what happened
once, not independent verification.

### What does it cost?

Dependency counts, version floors, whether a background process is needed and how
to remove it are in each repository's README under *Requirements* and *Status*,
and on
[the adopt page](https://openabstractions.org/adopt.html). Two costs that are
easy to miss:

- **Distribution is specific to the language and revision.** Read the language
  README and package metadata; verify the named artifact or tag exists before
  selecting it. A checkout that builds in our workspace does not establish that
  a package resolves independently.
- **Service availability is part of adoption.** The primary facade resolves a
  running service. Establish who installs and activates that runtime, how absence
  is reported, and which accepted work survives caller exit. Explicit legacy
  adapters have their own lifecycle and storage requirements.

### Which commits go together

[`layers.lock`](https://github.com/openabstractions/abstractions/blob/main/layers.lock)
retains an early eight-repository snapshot. It is historical evidence, not the
current runtime's dependency set. Use the selected module's versioned dependency
metadata and installation instructions. Runtime package versions and module tags
have separate release histories; qualify the combination your adopter ships.

### Licence

Read this repository's `LICENSE` and `NOTICE`, together with the selected
provider's dependency licenses. Do not infer a dependency's license from ours.

---

## Contributing

Public repositories are published from a maintained source tree. Some files are
copied source; others are generated codecs. External changes must be reconciled
with that source before publication. Work in this public repository: access to
the private tree is not a prerequisite for a useful patch.

This page is shared source published into several repositories.

### Where a fix goes

Use this repository's issue tracker or pull requests:

- **Open an issue here.** A description is enough; a diff in the body is
  better.
- **Open a pull request here.** Include the problem, the patch and relevant test
  results. Maintainers incorporate the accepted change into the source tree
  with attribution and link the resulting public commit.

For a generated codec defect, identify the definition or generator behavior
responsible and include a failing input. A generated-output diff can demonstrate
the correction, but the durable fix must regenerate it.

### Which files are generated

Codec headers identify generated code. Being copied into a public repository
does not make ordinary source code a generated codec. Do not infer ownership
from a commit subject or assume that license and build files are exceptions.
Maintainers resolve publication ownership when incorporating the patch.

### How to test a change

Start from the public repository root and read the affected language's README.
It supplies package-specific build, dependency and test commands. For a Go
module, enter its directory and run `go test ./...` with `GOWORK=off` to check
standalone dependencies. For generated bindings, change the schema or generator
and run its documented regeneration check. Run the relevant conformance
scenarios using the suite instructions above.

Report the exact commands, selected revisions, platform, outcomes and skipped
checks. A missing test entrypoint or unavailable toolchain is an explicit gap.
Include an outside-consumer check when changing packaging or public imports.

### What we owe you

If your change is right and we do not want it, you get a reason. If we take it,
you get the commit. If neither has happened in a fortnight, say so on the
issue — a generated repository has no maintainer watching it by habit, and that
is our problem to fix, not yours to work around.

---

## For maintainers writing a contract

A new or changed `.thrift` definition answers these eleven rules. Rules marked
(checked) are enforced by `idl/inventory.py`; existing definitions that break
them are listed in `idl/contract_rules.recorded`, and a new break fails the
check.

1. One outcome enum per call; refusals never travel as transport error codes.
   A call a policy may gate reserves `forbidden`, `unavailable` and `invalid`;
   a call addressing a record reserves `unknown`; a conditional write reserves
   `conflict`. Name the outcome of an unreachable decision point. (checked)
2. Every retained record states its identity and retry dimension, a retention
   the caller can read, loss as a typed terminal state, who may retire it, and
   what a replay reads after retirement.
3. A failure carries a class and a typed cause. Cause enums declare
   `unknown = "grant"` with `other`; a kind-specific cause has a namespaced
   string slot. (checked)
4. A catalogue of names applications will extend is open: `<owner>/<name>`,
   registered through a revision-conditional operator call, with the generated
   constant as a seed. A catalogue closed by rule says why with
   `closed_by = "<rule tag>"` and reserves the next name. (checked)
5. Every identity field names the party that asserts it and the mechanism that
   established it. The absent-claim case is defined, and a relay appends and
   never overwrites.
6. Bytes larger than one control frame cross a handoff or an advertised bound
   carried in the resource struct; a number in a doc string is a floor.
7. Enums a reader only displays are `unknown = "grant"`; enums a reader must act
   on are `unknown = "refuse"`; every enum says which. An incompatible record
   change is a new versioned key. (checked)
8. A rule names the entry point it judges; a second default for the same role
   gets its own rule.
9. `duplicate_keys = "refuse"`, unless the definition states why `last`.
10. The first definition of a contract records a "none" entry in the base
    protocol change log, and its review answers rules 1 to 6 in writing before
    generation.
11. Field, method and parameter names must not be reserved words in generated
    languages; a reserved name defines a per-language override such as
    `rust.name`.
