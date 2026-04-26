"""Top-level package for the SQL mutation testing framework.

The repository is organized around explicit pipeline stages so that benchmark
normalization, mutation generation, equivalence filtering, execution,
counterexample generation, augmentation, and reporting can evolve
independently. Counterexample generation, augmentation, and reporting are still
planned, but the package layout is already aligned with the full design.
"""

__version__ = "0.1.0"
