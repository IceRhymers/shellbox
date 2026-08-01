# ASD-STE100 — Rule Summary and Sources

This file summarizes the public description of ASD-STE100 (Simplified Technical English).
It paraphrases rule *categories*. It does not reproduce the standard's text or its
dictionary. For the authoritative document, request the free download from the official
site.

## What ASD-STE100 Is

ASD-STE100 is a controlled natural language. It was first released in 1986 as AECMA
Document PSC-85-16598, by what is now ASD (the AeroSpace and Defense Industries Association
of Europe). European airlines asked for it. Most of their maintenance staff were non-native
English speakers, and a misread instruction on an aircraft can kill people. The Simplified
Technical English Maintenance Group (STEMG) maintains the standard. It has been free to
download since Issue 6 (2013). The current edition is Issue 9 (January 2025).

## Structure

- **53 writing rules across 9 sections**, covering word choice, grammar, sentence
  structure, and style.
- **A dictionary** of roughly 900 approved words. Each word is restricted to one meaning
  and one part of speech. Roughly 1,200 more words are listed as words to avoid, with
  suggested replacements.
- **A terminology allowance.** An organization may define its own dictionary of approved
  technical nouns and verbs beyond the base list, for vocabulary the base list cannot
  cover.

## Rule Categories (Paraphrased)

**Word choice**

- Use an approved word only in its approved meaning and part of speech.
- Map each word to exactly one meaning. Do not rely on context to disambiguate a word with
  several senses.
- Prefer the plainer, shorter, more common word over a formal or rare synonym.

**Verb forms**

- Permitted forms are: infinitive, imperative, simple present, simple past, simple future,
  and past participle used only as an adjective.
- Compound and auxiliary constructions are not permitted. "We received" is allowed. "We
  have received" is not.
- An "-ing" form is permitted only as a technical noun, or as part of one. It is not
  permitted as a verb form.

**Voice**

- Procedures and instructions require the active voice.
- Descriptive text may use the passive voice, but only when the actor is genuinely unknown
  or irrelevant to the reader.

**Sentence structure**

- Write one instruction per sentence.
- Keep instructions to about 20 words. Keep descriptive text to about 25 words.
- Do not omit a verb, subject, or article to shorten a sentence. The standard warns
  explicitly that this creates ambiguity instead of clarity.
- Cap noun clusters at 3 words.

**Paragraph and document structure**

- Write one topic per paragraph.
- Keep a paragraph to about 6 sentences.
- Use a numbered or bulleted list for a sequence, a set of conditions, or a complex
  enumeration. Do not bury one in prose.

**Safety instructions**

- Open a safety-critical instruction with the command or the condition. Do not bury it
  mid-sentence.

## Why shellbox Repurposes This

STE was built for a reader who cannot ask a follow-up question. shellbox has two such
readers.

The first is an agent. shellbox is an MCP server, so an agent reads its tool descriptions
and its error payloads and decides what to call next. It has no back-channel. A passive
sentence that leaves the actor unstated — "the session is reaped" — forces that agent to
guess whether it or the server performs the action.

The second is a person reading this repo six months from now, who has a clone and nothing
else. That reader also cannot ask. This is why the standard's Tier 2 requires every
cross-reference to resolve from a clone alone.

## Where shellbox Departs From the Standard

shellbox's [`docs/writing-style.md`](../../../../docs/writing-style.md) is tiered. Strict
STE applies only to Tier 1, the text a machine parses. Tier 2 keeps emphasis and contrast,
because rationale prose has to convey which of two facts is load-bearing, and flat
declaratives cannot do that. ASD-STE100 has no such tier. It assumes every reader is
executing a procedure.

shellbox also does not use ASD's approved-word dictionary. It applies the underlying
principle instead: pick the plainest available word and use it the same way every time.

## Sources

- [ASD-STE100 official site](https://www.asd-ste100.org/)
- [ASD-STE100 — About STE](https://www.asd-ste100.org/about_STE.html)
- [ASD Europe — Simplified Technical English](https://www.asd-europe.org/standards-specifications/simplified-technical-english/)
- [Simplified Technical English — Wikipedia](https://en.wikipedia.org/wiki/Simplified_Technical_English)
- [TechScribe — ASD-STE100 Simplified Technical English](https://www.techscribe.co.uk/techw/asd-simplified-technical-english.htm)
- [SKYbrary — Simplified Technical English (STE)](https://skybrary.aero/articles/simplified-technical-english-ste)
- [danyuchn/asd-ste100-skill](https://github.com/danyuchn/asd-ste100-skill) — the skill this
  one adapts, MIT licensed.
