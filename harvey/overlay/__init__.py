"""harvey.db as the overlay on the registers (schema 25).

The registers (each a module's own file, attached read-only) describe every
firm; harvey.db keeps only what Harvey adds: which firms the user works,
websites and their trust, contacts and links, people Harvey found, searches,
merges with undo, sources, web signals and outreach.

- schema.py: the tables, dictionaries and indexes.
- triggers.py: the triggers that keep counts and current readings on
  row-at-a-time writes, and bulk mode (triggers off, one recount at the end).
- recount.py: every kept value computed again from base rows, and the diff
  (``harvey doctor --recount``).
- merges.py: merge and undo, which recount instead of relying on triggers.
- queries.py: the overlay's named page statements.

Nothing user-facing reads this schema yet: the current schema (24, state.py)
stays in use until the read and write paths move over.
"""
