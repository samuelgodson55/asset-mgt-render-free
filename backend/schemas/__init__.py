"""
schemas/
--------
Pydantic request/response models, grouped by the domain they belong to
(auth, assets, users, checkouts). Nothing in here talks to the database --
these are pure data-shape/validation definitions, imported by both
`api/*.py` (to type request bodies) and, where useful, `services/*.py`.
"""
