---
name: skill-creator
description: Write a new skill (or improve one) for this assistant - a reusable set of instructions saved with save_skill. Use when the user asks to make, add, teach or save a skill, or wants the assistant to remember how to do a kind of task every time.
---

# Making a skill

A skill is instructions you will follow every time a matching request comes in. It is saved as `SKILL.md` in the user's skills folder with `save_skill`.

## 1. Understand the job

Ask (with `ask_user`) only what you cannot work out: what the task is, what a good result looks like, any rules the user always wants followed (naming, style, which server, what not to touch). One or two concrete examples help most.

## 2. Name and description

- **name**: lowercase words joined by hyphens, short and specific - `backup-before-update`, `rank-chat-format`.
- **description**: one or two sentences saying what it does **and when to use it**, with the words a user would actually say. This is the only part you see before loading the skill, so it decides whether it is used. Under 1024 characters.

## 3. The instructions

- Steps in order, as commands to yourself: "Read X", "Check Y", "Run Z".
- Name the tools to use (`read_console`, `skript_reload`, `run_build`, ...) and how to verify the result.
- Put the user's rules in plainly, and the mistakes to avoid.
- Keep it short - well under a few hundred lines. Long reference material belongs in a separate file next to SKILL.md (the user can add files to the skill's folder), referred to by name.
- No secrets (passwords, tokens) in a skill.

## 4. Save and test

1. `save_skill` with the name, description and instructions - the user approves it.
2. Tell the user it is saved, that it is used automatically when a request matches, and that they can start it directly with `/<name>`.
3. Offer to try it on a real example.
