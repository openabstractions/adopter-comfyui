# Contributing

Most of the files here are generated. A script in another repository writes
them, copies them into this one and commits them with the subject `generated
from the private tree`. **A fix made to a generated file here is overwritten by
the next publication.** Until 2026-09-09 it was overwritten silently; now the
publisher refuses to run until somebody has dealt with your change. That is a
refusal, not a merge, so the next section is how a fix actually lands.

This file is one of the generated ones.

## Where a fix goes

The repository these files are generated from is private, and no amount of
asking will get you into it. We are not going to pretend otherwise. What works:

- **Open an issue here.** A description is enough; a diff in the body is
  better.
- **Open a pull request here.** We will not merge it as it stands, because
  merging it is what loses it. We apply the same change upstream, it comes back
  through the generator, and we close the pull request with a link to the
  commit that carries it. The upstream commit names you as its author.

Either route ends in the same place. Nothing else reaches these files at all.

## Which files are generated

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

## What we owe you

If your change is right and we do not want it, you get a reason. If we take it,
you get the commit. If neither has happened in a fortnight, say so on the
issue — a generated repository has no maintainer watching it by habit, and that
is our problem to fix, not yours to work around.
