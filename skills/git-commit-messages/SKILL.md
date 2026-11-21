---
name: git-commit-messages
description: Draft, review, or rewrite Git commit messages using staged changes, a user-supplied description, or an existing commit. Apply whenever proposing or creating a commit message.
---

# Git commit messages

## Determine what the message covers

- For a staged commit, inspect `git diff --cached --stat` and
  `git diff --cached` using the repository's actual Git directory and
  worktree. Describe the staged changes. If the index is empty, report that
  unless the user supplied a description or asked to use unstaged changes.
- For a message based on a supplied description, use that description. Keep
  claims within the evidence. For an existing commit, read its diff before
  rewriting the message.
- Drafting a message does not stage files, create a commit, rewrite history,
  or push. When the user asks to commit or rewrite messages, perform the
  requested operation within its stated scope.

## Write the message

1. Use a subject of at most **50 characters**. Capitalize its first word,
   use the imperative where natural, and omit the final period.
2. Keep the second line blank.
3. Wrap the body at **72 characters** per line. Explain what changes in
   behavior, capability, or workflow and why it matters. Leave mechanical
   detail to the diff.
4. Describe the outcome without generic addition verbs such as “Add”,
   “добавил”, or “добавлен”. Use the repository's language or the language
   requested by the user.
5. If one short subject cannot explain the whole change because it contains
   independent purposes, propose separate commits. Use `git add -p` to
   prepare separate changes or interactive rebase to split existing commits
   when the user requests such changes. Keep each commit coherent.

Check the final text against the diff or description, then count the subject
and body line lengths. Present a standalone draft in a code block so the
blank line and wrapping are preserved.

```text
Cache city buildings for offline map tiles

Store building outlines and height tags in a local snapshot so city
tiles can be calculated when Overpass is unavailable.
```

[How to Write a Git Commit Message](https://cbea.ms/git-commit/)
[The Art of Writing Meaningful Git Commit Messages](https://medium.com/@iambonitheuri/the-art-of-writing-meaningful-git-commit-messages-a56887a4cb49)
