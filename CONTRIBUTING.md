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

### Is it proven?

[The coverage grid](https://openabstractions.org/coverage.html) says which
implementation of which layer carries a verdict, in which language, on which
platform. Read the revision and scope attached to the verdict. `UNPROVEN`,
an unsupported capability and a passing result have different meanings.

A layer's own tests are a weaker claim than a conformance verdict. Both are
weaker than the same behaviour proven across two languages.

### What is it promising?

Each layer's `CONTRACT.md` — tagged rules, `[DL-R28]`, `[JOB-L5]` — is the
normative page, and it is tagged so a scenario can cite the exact rule it tests.
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
- **Bytes arriving while your application is closed is a separate install.** The
  in-process path does not claim it and refuses to pretend, which means the
  capability is a supervisor process you have to decide about.

### Which commits go together

[`layers.lock`](https://github.com/openabstractions/abstractions/blob/main/layers.lock)
pins the commit of each repository known to work with the others. It does not yet
have a row for every layer; where it has none, you are choosing the set yourself.

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

Run this repository's own tests — every language directory carries them — and
then the conformance suite above, because a change that keeps one implementation
happy and moves it away from the other two is the failure this project exists to
catch.

### What we owe you

If your change is right and we do not want it, you get a reason. If we take it,
you get the commit. If neither has happened in a fortnight, say so on the
issue — a generated repository has no maintainer watching it by habit, and that
is our problem to fix, not yours to work around.
