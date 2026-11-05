"""Collectors — deterministic gatherers that emit observations.

Each collector is independently runnable and re-runnable, states its cost up
front, and writes observations in batches (a long run that dies must not lose
what it already learned). Collectors only emit the signals of modules that are on.

Rule of thumb from the method this implements: if a fact can be obtained
deterministically, do not spend a model call on it.
"""
