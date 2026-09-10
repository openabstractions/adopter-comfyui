# Adopting, and contributing

The README of this repository is for a person deciding what this is. This page is
for whoever has to act on that decision: the first half is adopting it, the
second is changing it. Both link to the pages that hold the answers rather than
restating them, because a claim copied is a claim that goes stale.

## Adopting

**Start here: [what adopting actually involves](https://openabstractions.org/adopt.html)**
— run it, break it, call it from a program, implement it, and what the services
cost. It is the only page that answers "what do I install" honestly, including
where the answer is "nothing has been run from a clean machine, so no install
line is printed."

### Is it proven?

[The coverage grid](https://openabstractions.org/coverage.html) says which
implementation of which layer carries a verdict, in which language, on which
platform. Read it before depending on anything: most implementations in this
project carry no verdict attributed to the conformance tree, the page says so in
its own headline, and `UNPROVEN` and `—` must never be read as one.

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

- **A Python or C++ adopter is vendoring or pinning a commit, not adding a
  dependency.** Nothing of ours is on PyPI, and no C++ implementation has a
  tagged release. Each layer's `python/README.md` is the Python entry point:
  what to install, what to import, and one example that runs.
- **Bytes arriving while your application is closed is a separate install.** The
  in-process path does not claim it and refuses to pretend, which means the
  capability is a supervisor process you have to decide about.

### Which commits go together

[`layers.lock`](https://github.com/openabstractions/abstractions/blob/main/layers.lock)
pins the commit of each repository known to work with the others. It does not yet
have a row for every layer; where it has none, you are choosing the set yourself.

### Licence

Apache-2.0 throughout, one `LICENSE` at each repository root. Nothing copyleft
comes in.

---

## Contributing

Most of the files here are generated. A script in another repository writes
them, copies them into this one and commits them with the subject `generated
from the private tree`. **A fix made to a generated file here is overwritten by
the next publication.** Until 2026-09-09 it was overwritten silently; now the
publisher refuses to run until somebody has dealt with your change. That is a
refusal, not a merge, so the next section is how a fix actually lands.

This file is one of the generated ones.

### Where a fix goes

The repository these files are generated from is private, and no amount of
asking will get you into it. We are not going to pretend otherwise. What works:

- **Open an issue here.** A description is enough; a diff in the body is
  better.
- **Open a pull request here.** We will not merge it as it stands, because
  merging it is what loses it. We apply the same change upstream, it comes back
  through the generator, and we close the pull request with a link to the
  commit that carries it. The upstream commit names you as its author.

Either route ends in the same place. Nothing else reaches these files at all.

### Which files are generated

Two kinds live here.

- **Generated** — written upstream and published into this repository. The
  paragraph above is their whole story.
- **Authored here** — this repository is their only home and the generator does
  not know they exist. A pull request against one of these is an ordinary pull
  request and we merge it. `LICENSE`, `NOTICE`, `.gitignore` and
  `.gitattributes` are in this group in every repository; some repositories
  have more.

Ask git which kind a file is:

    git log -1 --format=%s -- <path>

`generated from the private tree` means generated. Any other subject means the
file was authored here.

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
