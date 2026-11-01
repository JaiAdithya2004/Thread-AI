# Thread — what it is, what it solves, and how its RAG works

## 1. What Thread is

Thread is a **shared memory for a software team**. It reads the places where decisions get made (email, calendar, repositories, chat, notes, and what team members tell their AI tools), pulls out the individual facts ("we chose Postgres", "the launch moved to 14 March", "Priya owns billing"), keeps track of which facts are still true, and answers questions with citations to the evidence.

You can ask it from a web app, from Claude Code or any MCP client, from a command line, or from Slack.

## 2. The problem

Teams lose their own decisions.

- **Decisions are scattered.** They happen in a thread, a meeting, a pull request, a DM. Nobody writes them down in one place.
- **Facts change.** "Budget is 2M" becomes "budget is 3M" three weeks later. A search engine finds both and does not say which is current.
- **People leave and new people arrive.** The reasoning goes with the person.
- **AI assistants start every session blank.** An assistant that does not know what the team already decided will suggest the option you rejected last month.
- **Plain document search returns documents, not answers.** You get ten links and read them yourself.

## 3. What Thread does about it

| Need | What Thread does |
|---|---|
| Find what the team decided | Retrieves facts, not documents, and answers in a sentence or two with citations |
| Know what is current | When a new fact replaces an old one, the old one is kept but marked superseded, so answers use the current value |
| Know how it changed | "How did the budget change?" returns the timeline, old values included, labelled |
| Not say things it cannot back up | Abstains when the evidence is missing; every claim carries a fact id |
| Keep private things private | One person's mailbox facts are visible to that person only, even to admins |
| Give AI tools a memory | An MCP server and Claude Code hooks read and write the same store |
| Be auditable | A ledger records who asked what and which facts were returned |

**Who it is for:** small engineering and product teams who already work across several tools, and anyone building agents that need a durable, checkable memory. It is a portfolio project: it is built and measured to production standards in places, but has not been run with real customers.

**What it is not:** a general document search, a wiki, a note-taking app, or a replacement for the source tools. It does not store your files; it stores short extracted statements with a pointer back to where they came from.

## 4. How RAG works, briefly

RAG ("retrieval-augmented generation") means: **look up relevant material first, then have the model answer using only that material.** Language models do not know your company and will invent plausible answers if asked. RAG replaces guessing with reading.

The usual recipe:

1. **Split** documents into chunks.
2. **Embed** each chunk: a model turns text into a vector so that similar meanings sit close together.
3. **Store** vectors in a vector index.
4. At question time, **embed the question**, find the nearest chunks, and **paste them into the prompt** with the instruction "answer from this".

Its weaknesses are well known: chunks cut facts in half; old and new versions of the same thing both match; there is no notion of who may see what; and the model may still wander beyond what it was given.

## 5. How Thread's RAG differs

Thread indexes **facts**, not chunks.

```
 message ──► scrub ──► extract facts ──► embed ──► reconcile ──► store
                       (LLM)             (768-d)   vs. related     facts + vectors
                                                    existing facts  + FTS index
                                                    (LLM judge)

 question ──► embed ──┬─► vector search (pgvector, HNSW) ─┐
                      └─► full-text search (Postgres FTS) ─┴─► RRF fusion
                                   ──► access filter ──► top 5 (8 for history)
                                   ──► answer with citations ──► output check ──► ledger
```

| Stage | What happens | Why |
|---|---|---|
| **Scrub** | Instruction-like text in ingested messages is neutralised | A message is data; it must not be able to command the model |
| **Extract** | A model turns each message into short, self-contained statements (subject, predicate, object, summary) | A fact is a better retrieval unit than a paragraph: it cannot be cut in half and it can be compared with other facts |
| **Embed** | Gemini embedding, 768 dimensions | Meaning-based match, so "pricing" finds "cost per seat" |
| **Reconcile** | The new fact's nearest existing facts are shown to a judge model, which decides: duplicate, supersedes, contradicts, or unrelated | This is how Thread knows what is current. Similarity alone cannot tell "same fact restated" from "same topic, new value" |
| **Retrieve** | Vector and keyword search run together and are merged by reciprocal rank fusion | Vectors catch paraphrase; keywords catch exact names and numbers. Keyword search alone found only 24% of answers in the benchmark |
| **Filter** | Team, visibility and access-list checks are applied after ranking | No ranking path can skip the permission check |
| **Answer** | A model sees the top facts with dates and "superseded" labels, plus today's date | So it can say "as of March, now replaced by..." |
| **Check** | The answer is scanned for secrets and for attempts to jailbreak | A last guard before anything leaves |
| **Log** | Question, caller and fact ids go in the ledger | Auditing |

Why the reconcile step matters most: in the controlled benchmark, a search-only system retrieved the right fact but answered with the stale value for every changing fact (0 of 42 correct). With reconcile on, 41 of 42 were correct.

**What is not in the design:** conversational memory (each question is answered independently), chunking of long documents (a very long document is truncated before extraction), and a reranker (implemented, switched off by default).

## 6. Does LangChain fit?

**Thread does not use LangChain, and it does not need it today.** Calls to models go through LiteLLM (one interface to Gemini, OpenRouter and others); retrieval is SQL against Postgres; the pipeline is a handful of functions with explicit control flow. LangChain would wrap things that are already thin.

| Question | Answer |
|---|---|
| Would it add capability? | Very little. Its main value is ready-made connectors, text splitters and agent loops; Thread uses Composio for connectors and has no agent loop |
| What would it cost? | A large dependency tree. The hosted function runs on Vercel with a 50 MB limit and a deliberately small `api/requirements.txt`; LangChain would put that at risk. It also abstracts exactly the parts (prompts, retries, fallbacks) that Thread needs to control and test |
| Where could part of it help? | `langchain-text-splitters` alone is a light, sensible way to add **chunking for long documents**, which Thread lacks. That is a reasonable future addition |
| What about LangGraph? | Useful if Thread grew a multi-step agent (research, then verify, then answer). The current answer path is one retrieval and one generation, so it is not needed |
| Is it a good thing to learn for a portfolio? | Mentioning it helps, but showing that you can build the pipeline without it and measure it is the stronger signal. Do not add it for its own sake |

**Recommendation:** leave it out. Revisit only for the text splitter, or if an agent workflow is added.

## 7. Does Langfuse fit?

**Langfuse is the right kind of tool for this system, and it is already wired in, but as shipped it does not work.** Langfuse records each model call (prompt, response, latency, tokens, cost) so you can see why an answer was wrong and attach scores to it. For a RAG system that is valuable.

Current state, verified by reading the code and the installed SDK:

| Problem | Detail | Fix |
|---|---|---|
| Not deployed | `langfuse` is not in `api/requirements.txt`, so the hosted function never loads it | Add it (small package) |
| Wrong API | `brain/core/llm.py` calls `Langfuse.trace()`, which was removed in SDK 4.x; the call fails and the error is swallowed, so nothing is recorded and nothing complains | Move to the 4.x API (`start_as_current_span` / `update_current_generation`), or use LiteLLM's built-in `langfuse` callback |
| Never flushed | Events are queued in memory and a serverless function freezes before they are sent | Call `flush()` at the end of each request |
| Privacy | Full prompts include retrieved facts, which can include a person's private mailbox facts | Mask private content before sending, or self-host Langfuse |

Where it would fit in Thread's flow: one trace per question, with spans for embed, retrieve, rerank and synthesise, and a score attached for each answer judged by the evaluation harness. That ties offline evaluation to live traffic.

**Recommendation:** worth finishing, in this order: use LiteLLM's callback (smallest change), add the dependency, flush per request, mask private facts. Until then, describe it as **planned**, not as a feature. Langfuse complements the benchmark; it does not replace it, because it observes real traffic and cannot tell you the right answer unless someone labels it.

## 8. Related reading

- [SYSTEM_BOUNDARIES.md](SYSTEM_BOUNDARIES.md): exactly what is and is not implemented
- [ARCHITECTURE.md](ARCHITECTURE.md): components and data flow
- [../README.md](../README.md): setup and the full evaluation tables
