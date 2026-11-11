# Directive

The restock and pricing directive must let a person who has never seen our code load every slot and set every price without asking a question.

**What the brief requires** (Project Brief (Revised), section 2, job 4): a clear, human-readable restock and pricing directive saying which product goes in which row, how many, and at what price. Someone who has never seen our code must be able to act on it without asking us anything. The brief stresses this is not paperwork: it is where the agent meets the physical world, and output a person cannot carry out has managed nothing.

**What a good directive has**, for every slot we touch:

- the slot number, exactly as on the machine;
- the product: brand, flavor and size, spelled the way the catalog spells it;
- how many units to load, or that the slot stays as it is;
- the price to set on the tag, in dollars and cents;
- for a product change, what to do with the units being removed.

**Rules of thumb.**

- One line per slot. No abbreviations the worker would not know.
- No conditions for the worker to work out. A fallback product belongs in the alternate, which the tool already handles.
- No internal names, IDs or code terms.
- Prices in 5-cent steps.

**How this works for us.** `preview_desired_state` writes the worker's directive from the desired state. Read it before submitting and check it against the list above. If any line would leave the worker with a question, fix the desired state rather than adding an explanation.

A sample directive is also a graded deliverable (section 5).
