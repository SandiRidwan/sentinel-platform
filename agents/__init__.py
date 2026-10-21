"""A governed question-answering agent over the gold layer.

The point of this package is not that a model can answer a question about books. It is
that the answer is produced by a pipeline where every step is bounded:

    plan     choose a table from the registry, or refuse
    act      build SQL from validated parts (the model never writes SQL)
    verify   check the result actually answers the question, and refuse if it is empty

`agents/eval.py` runs 20 questions that must be answered and 10 that must be refused,
and fails if either group is wrong. An agent that answers everything is not better than
one that refuses correctly; it is worse, because the wrong answers look like the right
ones.
"""
